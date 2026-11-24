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

- `u_ff1` : feed-forward structurel (puissance de maintien), dérivé de $b/a$ ou du modèle de pièce publié,
- `u_pi` : terme PI discret.

La sortie injectée dans le calcul est `u_ff1 + u_pi`, puis elle passe par :

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

- `AB_MIN_SAMPLES_B = 5`,
- `AB_MIN_SAMPLES_A = 5`,
- `AB_A_SOFT_GATE_MIN_B = 5`.

SmartPI quitte `HYSTERESIS` lorsque `b` dispose de 5 mesures et que `a` est publié.
`a` est publié à 5 mesures, ou dès `AB_MIN_SAMPLES_A_CONSISTENT = 4` mesures lorsqu'elles
concordent : leur écart absolu médian est au plus `AB_A_CONSISTENT_MAX_REL_MAD = 0.10`
de leur médiane. La première publication de `a` attend aussi le modèle de puissance
délivrée (3.7) : tant que le retard de l'émetteur n'est pas résolu, les mesures sont
collectées sans publication, jusqu'à ce que la collecte atteigne `AB_HISTORY_SIZE = 31`
mesures : la mesure admise suivante publie alors `a`. Les deux buffers continuent ensuite à se remplir jusqu'à
`AB_HISTORY_SIZE = 31` pendant la régulation SmartPI normale. Après un redémarrage,
l'état restauré du retard de l'émetteur fixe la phase dès le premier calcul ; un état
sauvegardé sans bloc de retard de l'émetteur attend la première mise à jour de l'apprentissage.

Dans la bande d'hystérésis, la sortie conserve son état tout-ou-rien précédent. Une
sortie modulée laissée par une autre phase n'est pas un état d'hystérésis : elle est
ramenée à 0 jusqu'au prochain seuil. L'état tout-ou-rien est persisté avec l'heure
murale du dernier calcul. Après un redémarrage ponté (5.4), le premier calcul
disposant de la température intérieure le reprend dans la bande, comme l'aurait fait une hystérésis jamais interrompue ; en attendant, l'état
tout-ou-rien d'avant l'arrêt est conservé, comme décrit dans Redémarrages.
Hors de la bande, les seuils décident. Après tout autre redémarrage, et pour un
état sauvegardé sans cet état, la sortie repart à 0. Une phase de chauffe ou de
refroidissement en cours peut ainsi reprendre après un redémarrage court.

Les phases de chauffe de l'hystérésis sont des événements de mesure : elles sont
enregistrées et lues dans leur ensemble, et à la fin du bootstrap les lectures
donnent à l'apprentissage des valeurs de départ pour `a` et `b` (3.7, Événements
de mesure et valeurs de départ). La commande d'hystérésis, l'ordre ci-dessus et
la règle de sortie sont les mêmes avec ou sans elles.

Cet ordre est celui du démarrage par amorçage. Avec le démarrage direct (option
`smart_pi_learning_start`, 3.7, Démarrage direct), l'étape `HYSTERESIS` est la même étape d'apprentissage,
mais la commande est le PI et l'étape attend `b` jusqu'à ce qu'une valeur de départ de `b` tienne lieu
ou que la constante de temps propre de l'apprentissage ait été fiable. L'option n'agit que sur un
démarrage vierge : un état avec des mesures, des valeurs de départ, des impulsions ou des lectures garde
le démarrage avec lequel son apprentissage a commencé (le démarrage direct est sauvegardé sous
`learning_start` ; un état sans cette clé a commencé par l'amorçage), et une réinitialisation de
l'apprentissage reprend le démarrage configuré.

La convergence de `b` utilisée pour l'apprentissage `a` avec détection de dérive repose sur `b_converged_for_a()` :

1. `learn_ok_count_b >= 11`,
2. `len(_b_hat_hist) >= 5`,
3. `MAD(b_hat)/Med(b_hat) <= 0.30`,
4. `range(last_5_b_hat)/Med(b_hat) <= 0.10`.

### 3.2 Fenêtre d'apprentissage réelle

Le code n'utilise plus de constante `WINDOW_MIN_MINUTES`. L'apprentissage repose aujourd'hui sur `LearningWindowManager` et sur une fenêtre glissante qui :

- exige une température extérieure disponible,
- respecte la gouvernance thermique,
- rejette ou réinitialise une fenêtre ON/`a` sur l'observation exacte du changement
  de consigne utilisateur, afin qu'une fenêtre ne puisse pas couvrir les deux
  régimes,
- conserve l'éligibilité de la montée active initiale suivant cette observation
  pour `a`, sous les garde-fous existants de temps mort, stabilité de puissance,
  pente robuste et gouvernance,
- conserve l'apprentissage passif OFF/`b` sous ses garde-fous physiques existants ;
  les réponses dues uniquement à une perturbation ne sont pas bloquées par ce gate
  de consigne,
- bloque la collecte en bootstrap tant que les temps morts requis ne sont pas fiables ; dans l'étape d'apprentissage du démarrage direct, une fenêtre `b` exige à la place le pic confirmé de l'arrêt (3.7, Démarrage direct) ou un retard de l'émetteur connu (publié, ou retenu par les lectures des impulsions), avec lequel elle débute `3 T_ar` après la coupure,
- applique une pause après reprise (`LEARNING_PAUSE_RESUME_MIN = 20`),
- ancre le début de fenêtre après la fin du temps mort quand c'est nécessaire,
- retarde le début de fenêtre jusqu'à l'extinction de la réponse transitoire (`skip: response transient (a|b)`) : une fois le retard de l'émetteur publié (3.7), une fenêtre `a` débute au moins `min(3 T_ar, moitié de l'épisode actif médian)` après l'échelon de puissance ; une fenêtre `b` débute après la coupure de puissance au moins du plus long de `3 T_ar` (retard de l'émetteur publié) et de `AB_SETTLE_K_PASSIVE_DEADTIME = 1.5` temps morts passifs fiables : l'émetteur continue de chauffer après une coupure, et comme la pièce ne commence à se refroidir que lorsque cette chaleur résiduelle passe sous les pertes, le temps mort passif borne sa décroissance même lorsque le retard de l'émetteur est inconnu. Pendant le bootstrap, seul le terme `3 T_ar` s'applique,
- ne soumet pas une fenêtre active plus courte qu'un cycle PWM (`active_window_shorter_than_cycle`) : sa durée est fixée avant ses données, car arrêter dès qu'une fenêtre courte paraît significative favorise les pentes les plus fortes parmi les données bruitées. Pendant le bootstrap, le chauffage fonctionne à pleine puissance entre les seuils d'hystérésis et une phase de chauffe peut être plus courte qu'un cycle : le minimum est alors plafonné à la moitié de la phase de chauffe médiane (`AB_SETTLE_ACTIVE_CAP_FRAC`), mesurée sur les phases précédentes,
- clôt la fenêtre à sa dernière observation lorsque deux mises à jour ne sont pas contiguës (intervalle plafonné à un cycle) : la fenêtre est soumise sur les données jusqu'à cet instant lorsque sa pente y est robuste, sinon abandonnée (`skip: observation gap`) ; elle ne couvre jamais la période non observée,
- surveille la stabilité de puissance via le coefficient de variation (`U_CV_MAX`),
  évalué dès que la puissance moyenne de la fenêtre atteint `U_CV_MIN_MEAN = 0.05`
  (en dessous, une puissance qui revient dans une fenêtre OFF n'est pas détectée par ce contrôle),
- prolonge la fenêtre tant que la pente n'est pas jugée robuste,
- abandonne la pente après `DT_MAX_MIN = 240` minutes si aucun signal robuste n'émerge.

En pratique, la décision de soumettre ou d'étendre la fenêtre dépend de la robustesse de la pente, pas d'une durée minimale fixe. Une fenêtre fermée sans pente robuste n'est pas perdue : son bilan intégral alimente les pools décrits en 3.5.

### 3.3 Estimation robuste de pente

`ABEstimator.robust_dTdt_per_min()` applique des garde-fous sur la pente :

- amplitude minimale : `DT_DERIVATIVE_MIN_ABS = 0.05°C`,
- nombre minimal de changements de niveau : `OLS_MIN_JUMPS = 3`,
- significativité statistique : `OLS_T_MIN = 2.5`,
- étendue minimale : une fois la pente significative, la variation ajustée sur la
  fenêtre (`|pente| × durée`) doit couvrir au moins `OLS_MIN_SPAN_STEPS = 1.5` pas
  de capteur ; le pas est le plus petit changement observé dans la fenêtre, avec un
  plancher `OLS_SPAN_MIN_STEP_C = 0.05°C` qui sert de marge de bruit pour les
  capteurs à résolution fine. Sinon la fenêtre est prolongée
  (`span_below_resolution`).

La pente étant testée à chaque mise à jour, une fenêtre trop courte au regard de la
quantification serait soumise dès que sa pente paraît significative : avec un
capteur de 0,1 °C qui bascule entre deux niveaux et une pièce qui refroidit
lentement, l'échantillonnage déclenché par les publications domine alors
l'ajustement et `b` peut être faux de ±50 %. Sur une fenêtre couvrant plusieurs pas,
la pente est sans biais quel que soit le profil du capteur.

Les pentes sont calculées sur la température intérieure brute mesurée, et non sur les signaux filtrés, car le garde-fou de robustesse compte les sauts de valeur distincts de la mesure. L'historique brut (et l'historique filtré utilisé par la détection du temps mort) garde les échantillons des `TIN_HISTORY_RETENTION_MIN = 300` dernières minutes (`DT_MAX_MIN` + 60), dans la limite de `TIN_HISTORY_MAX_SAMPLES = 1800` échantillons. Un échantillon est ajouté à chaque changement de valeur ou après 60 s : toute la durée est couverte tant que les échantillons arrivent en moyenne au plus toutes les 10 s ; au-delà, le plafond d'échantillons raccourcit la durée couverte (environ 150 minutes avec un échantillon différent toutes les 5 s) et la pente d'une longue fenêtre n'utilise que ses échantillons les plus récents. Les pentes validées alimentent ensuite `ABEstimator.learn()` qui :

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

Pour une fenêtre active, `u` est la puissance délivrée moyenne de la fenêtre (3.7), et non la commande appliquée.

Chaque mesure de `a` conserve son observation (`dT/dt`, `T_int - T_ext`, `u`). À
chaque publication d'une nouvelle valeur de `b`, toutes les mesures conservées sont
recalculées avec ce `b`, puis `a` est agrégé à nouveau s'il a déjà été publié : une
mesure de `a` faite avant que `b` soit précis suit donc ses corrections. Une mesure
restaurée sans observation, ou dont le recalcul contredirait le signe du mode,
garde sa valeur.

### 3.5 Bilan intégral des fenêtres sans pente robuste

Avec un capteur grossier (pas de 0,2 °C) ou publiant rarement, une phase de
chauffe ou de refroidissement contient rarement les trois sauts de valeur
exigés par la pente OLS. Le modèle intégré entre deux instants donne pourtant
l'information sans pente significative :

$$ T(t_2) - T(t_1) = a \cdot U - b \cdot D, \quad U = \int u\,dt, \quad D = \int (T_{int} - T_{ext})\,dt $$

`ABWindowBalance` (`ab_window_balance.py`) suit chaque fenêtre acceptée :

- il enregistre à chaque pas de la température brute les intégrales cumulées
  de `u`, de `T_int - T_ext` et de `T_ext` ;
- à la fermeture d'une fenêtre par une garde, un timeout ou l'absence de pente
  robuste, il retient le segment entre le premier et le dernier pas : `b` si la
  puissance moyenne est inférieure à `U_OFF_MAX`, `a` si elle dépasse `U_ON_MIN` ;
- une fenêtre soumise par la pente OLS n'est jamais réutilisée, et une
  réinitialisation externe (reprise, apprentissage désactivé) l'écarte ;
- un timeout de pente ne ferme pas le segment, qui se poursuit dans la
  fenêtre suivante jusqu'à une garde.

Un segment n'est retenu que si :

- tous les pas de la fenêtre vont dans le sens de la réponse (sinon le
  capteur est bruité à l'échelle du pas et la fenêtre reste du ressort de l'OLS),
- il dure au moins `AB_BALANCE_MIN_SEGMENT_MIN = 15` minutes,
- la puissance n'a pas changé pendant la collecte : celle-ci s'arrête au
  premier tick qui s'écarte de la puissance moyenne, afin que l'inertie de
  l'émetteur après une coupure ne soit pas attribuée à la puissance précédente,
- pour `b`, les gardes de gradient intérieur/extérieur et de marge extérieure
  de la fenêtre passive sont respectées.

Les deux extrémités sont des pas dans le même sens. Un capteur qui publie au
changement franchit alors le même seuil de quantification, et la variation
mesurée est exacte ; un capteur échantillonné périodiquement fournit des valeurs
à un demi-pas près. Le modèle d'incertitude retient un demi-pas par extrémité :

$$ \sigma_{rel} = \frac{q \sqrt{n / 6}}{|N|} $$

avec `q` le plus petit pas observé (borné à `AB_BALANCE_MIN_RESOLUTION_C = 0.05`,
`AB_BALANCE_DEFAULT_RESOLUTION_C = 0.1` tant qu'aucun pas n'a été vu), `n` le
nombre de segments et `N` le numérateur (`ΔT + b·D` pour `a`, `ΔT` pour `b`).

Les segments sont cumulés par paramètre (au plus `AB_BALANCE_POOL_MAX_SEGMENTS = 12`,
âgés d'au plus 24 h, vidés au changement de mode HVAC). Dès que
`σ_rel <= AB_BALANCE_MAX_REL_UNC = 0.25`, le pool est soumis à `ABEstimator.learn()`
comme une mesure unique, avec la pente, la puissance et l'écart
intérieur/extérieur équivalents (moyennes sur la durée cumulée) ; l'agrégation
médiane/MAD, la détection de dérive et le soft gate s'appliquent sans changement.
Le pool `a` attend que `b` dispose de `AB_A_SOFT_GATE_MIN_B` mesures. Le seuil
est plus strict que celui de la pente OLS (`t >= 2.5` accepte une erreur
relative jusqu'à 0,40).

### 3.6 Estimation du temps mort

`DeadTimeEstimator` implémente une FSM indépendante. Les canaux décrivent la
réponse physique de la pièce, pas le sens de la transition de puissance :

- `deadtime_heat` : temps mort avant une montée de température,
- `deadtime_cool` : temps mort avant une baisse de température.

Ainsi, OFF -> ON apprend `heat` en mode HEAT mais `cool` en mode COOL.
La transition ON -> OFF apprend la réponse passive opposée.

La détection suit la température intérieure filtrée, en régulation comme pendant le bootstrap et les impulsions de mesure : une mesure brute qui oscille d'un pas de capteur détecterait sinon une réponse dès l'échelon de puissance. Une détection en cours reprend après un redémarrage ponté (5.4) : son état, son début, son creux ou son pic, la tendance avant le démarrage, la dernière puissance et la dernière coupure de puissance sont persistés en temps mural, et son délai d'expiration continue de courir depuis le début sauvegardé. Après tout autre redémarrage, elle est abandonnée, et la durée minimale OFF doit être observée à nouveau dans la session en cours avant qu'une détection puisse être armée.

Une réponse est détectée dès que la température s'est écartée de `0.05 °C` de son creux (chauffe) ou de son pic (refroidissement) depuis la transition de puissance. Le temps mort court de la transition au dernier instant à moins de `0.01 °C` de ce creux ou de ce pic, suivi pendant l'attente : il ne dépend donc pas de la rétention de l'historique de température. L'attente abandonne après `DEADTIME_ACTIVE_TIMEOUT_S = 14400 s` pour une réponse active (puissance démarrée) et `DEADTIME_PASSIVE_TIMEOUT_S = 43200 s` pour une réponse passive (puissance coupée) : après une coupure, un émetteur qui stocke beaucoup de chaleur (plancher chauffant) continue de réchauffer la pièce, puis plafonne, et la baisse peut arriver plusieurs heures plus tard. Pendant le bootstrap, l'attente passive se termine par une détection ou par une reprise de chauffe, qui exige une baisse que la détection voit d'abord ; la limite ne fait que borner le délai au-delà duquel une baisse n'est plus attribuée à la coupure. Une transition de puissance abandonne une attente en cours. L'issue de la dernière attente de chaque réponse et l'âge de l'attente en cours sont publiés (`analysis.deadtime`).

La réponse à un démarrage de puissance est l'écart de la température par rapport à l'évolution qu'elle suivait avant le démarrage. Sans chauffe, la température d'une pièce reste stable ou baisse : le suivi du creux ci-dessus date alors la réponse. Une pièce qui se réchauffe déjà avec le chauffage éteint (occupants, soleil, pièce voisine) s'éloigne par elle-même du niveau de démarrage, et le suivi du creux daterait la réponse trop tôt (en COOL, il en va de même pour une pièce qui se refroidit déjà avant le début du refroidissement). Ainsi, à un démarrage actif (puissance activée), `deadtime_trend.prestart_trend()` ajuste une droite sur la température brute de la période OFF précédant le démarrage :

- fenêtre : les `DEADTIME_PRESTART_WINDOW_S = 1800 s` précédant le démarrage, commençant au plus tôt à la dernière coupure de puissance augmentée du temps mort passif (l'émetteur agit encore sur la pièce avant ce délai) ;
- avec moins de `DEADTIME_PRESTART_MIN_SPAN_S = 900 s` ou moins de `DEADTIME_PRESTART_MIN_SAMPLES = 4` échantillons, aucune tendance n'est retirée ;
- une tendance dont la variation ajustée reste dans un pas de capteur (la plus petite variation observée dans la fenêtre, au moins `DEADTIME_PRESTART_STEP_FLOOR_C = 0.01 °C`), ou qui va à l'encontre de la réponse, n'est pas non plus retirée : la détection est le suivi du creux (ou du pic) ci-dessus, inchangé. C'est le cas habituel pendant le bootstrap, où la chauffe reprend parce que la pièce s'est refroidie ;
- une tendance dans le sens de la réponse est ajustée sur toute la fenêtre et sur sa moitié récente (au moins `DEADTIME_PRESTART_MIN_HALF_SAMPLES = 3` échantillons par moitié). La détection est rejetée si la tendance n'est pas une droite : un résidu rms supérieur à la moitié du seuil de détection (et supérieur à un pas de capteur), ou une pente qui, de la première moitié à la seconde, croît dans le sens de la réponse de plus du seuil de détection par demi-fenêtre. Une droite sous-estimerait alors la tendance au démarrage ;
- sinon, le détecteur suit la température moins la tendance extrapolée, avec la pente la plus forte des deux ajustements : en retirer trop ne fait que retarder la détection. Le creux, le seuil de `0.05 °C` et la rétrospection de `0.01 °C` s'appliquent à cette différence ;
- au moment de la détection, la mesure est rejetée si la tendance seule couvre au moins le seuil de détection depuis le démarrage : la réponse n'est alors pas séparable de la tendance.

Une mesure rejetée publie l'issue `rejected` et laisse les détections inchangées : le temps mort conserve sa valeur précédente. Une réponse passive (coupure de puissance) n'est pas concernée : la tendance précédant une coupure est celle de la chauffe elle-même.

Une mesure qui ferait baisser le temps mort publié est aussi ignorée (issue `ignored`, le temps mort garde sa valeur) lorsque la preuve est faible : la tendance avant le démarrage va nettement dans le sens de la réponse (variation ajustée d'au moins `DEADTIME_PRESTART_RISE_MIN_STEPS = 2` pas du capteur sur la fenêtre, même si elle a été retirée plutôt que rejetée), ou la fenêtre est trop courte pour évaluer une tendance. Une pièce qui se réchauffe avant le démarrage ne peut que faire paraître la réponse plus précoce : elle ne raccourcit donc jamais le temps mort. Une mesure égale ou plus longue que la valeur publiée suit les règles ci-dessus, de même que toute mesure avant la première valeur publiée.

La fiabilité devient vraie dès qu'au moins une mesure valide a été capturée sur le canal correspondant. Chaque canal garde ses 6 dernières détections ; la valeur publiée est leur médiane, de sorte qu'une détection aberrante isolée ne la déplace pas (avec une ou deux détections, la médiane vaut leur moyenne). Au redémarrage, la valeur publiée est recalculée à partir des détections restaurées.

### 3.7 Retard de l'émetteur et puissance délivrée

À l'échelle d'une fenêtre d'apprentissage, la pièce se comporte comme un intégrateur derrière des inerties rapides (actionneur, émetteur, brassage de l'air, boîtier de la sonde) et un temps mort pur (réponse intégratrice du premier ordre avec temps mort). Après un échelon de puissance, la pente se stabilise progressivement et son asymptote de rampe croise la trajectoire passive extrapolée à $t = T_{ar} = L + \sum \tau_i$, le temps de séjour moyen de la réponse (Åström et Hägglund), le retard de l'émetteur. Une fenêtre qui débute $k \cdot T_{ar}$ après l'échelon présente un déficit de pente d'au plus $e^{-k}$ pour une inertie unique, quelle que soit la part du temps mort pur. Une fenêtre comprise dans le régime transitoire sous-estime `a`, d'autant plus que le retard de l'émetteur est long ou que la sonde rapporte en retard.

`ResponseLagEstimator` (`response_lag.py`) mesure $T_{ar}$ sur les épisodes actifs à puissance constante (écart d'au plus `RESPONSE_LAG_U_TOL = 0.05`) :

- l'épisode doit suivre une période passive d'au moins `max(RESPONSE_LAG_MIN_OFF_S = 1800 s, RESPONSE_LAG_MIN_OFF_LAGS = 5 × retard de l'émetteur connu)`, afin que la puissance de l'épisode précédent soit retombée ;
- la référence passive est la pente OLS robuste de la fin de cette période OFF (sur `RESPONSE_LAG_PASSIVE_SPAN_S = 3600 s` au plus), à défaut le `b` publié ;
- la droite tardive est ajustée sur les franchissements de niveau de l'historique brut (au moins `RESPONSE_LAG_MIN_CROSSINGS = 3`). Pour un saut d'un pas de capteur, le franchissement est daté au milieu des deux échantillons ; un saut de plusieurs pas conserve la valeur brute. Cela supprime les biais en escalier et de seuil de déclenchement des capteurs qui rapportent sur changement ;
- l'ordonnée à l'origine d'une droite OLS ajustée sur un segment transitoire tombe avant $T_{ar}$ : l'estimateur inverse la forme close de l'ordonnée à l'origine d'une réponse à inertie unique sur l'intervalle ajusté (monotone en $T_{ar}$). Un épisode n'est pas utilisé lorsque l'inversion est trop plate (`RESPONSE_LAG_MIN_INVERSION_SLOPE = 0.05`) ou lorsque l'ordonnée à l'origine atteint `RESPONSE_LAG_RESOLVABLE_FRAC = 0.9` de sa limite pour une inertie infinie. Une estimation au-delà de `RESPONSE_LAG_MAX_S` (`DT_MAX_MIN` × 60 = 14 400 s) est également écartée (`intercept not resolvable`) ;
- chaque estimation porte son écart-type, propagé à partir des résidus de l'ajustement et d'une incertitude relative supposée de `b` égale à `RESPONSE_LAG_B_REL_UNC = 0.10`.

Les `RESPONSE_LAG_HISTORY = 7` dernières estimations sont conservées. Le retard de l'émetteur est publié lorsqu'au moins `RESPONSE_LAG_MIN_ESTIMATES = 3` estimations donnent un écart-type combiné de leur médiane pondérée par l'inverse des variances au plus égal à `RESPONSE_LAG_MAX_REL_UNC = 0.15` de cette médiane (plancher `RESPONSE_LAG_UNC_FLOOR_S = 30 s`) ; les estimations situées à plus de trois écarts-types de la médiane ne sont pas combinées. Un ensemble incohérent conserve la valeur publiée, afin que des estimations dispersées n'activent et ne désactivent pas le modèle de puissance délivrée. Une discontinuité de plus de 1 s entre deux mises à jour termine l'épisode au dernier instant observé, et la coupure de puissance est datée à la reprise.

Un redémarrage ponté (5.4) reprend la période passive ou l'épisode actif en cours lors de la sauvegarde. Le contexte est persisté en temps mural (dernier instant observé, puis début de la période passive, ou début, puissance, constance, durée passive préalable et température extérieure de l'épisode) et reconverti en horloge monotone au chargement. Le chauffage a conservé la puissance engagée pendant l'arrêt, et la première mise à jour après le redémarrage couvre l'arrêt avec cette puissance : une période passive se poursuit, et l'arrêt compte comme passif ; un épisode se poursuit, et la première observation le prolonge ou le termine comme toute autre. Après tout autre redémarrage, la période passive est abandonnée et un épisode se termine sans estimation (`episode interrupted by restart`) ; une puissance non nulle ouvre alors un nouvel épisode sans période passive connue. Les franchissements viennent de l'historique brut du détecteur de temps mort, persisté de la même façon ; il ne contient aucun échantillon pendant l'arrêt. Un état sauvegardé sans ce contexte ne reprend rien.

L'inversion suppose une réponse à inertie unique (premier ordre). Elle ne restitue le temps de séjour moyen que pour un tel émetteur, ou lorsque l'épisode dure plusieurs fois le retard de l'émetteur. Sur un émetteur qui combine un temps mort pur et une inertie, ou deux inerties successives, l'ordonnée à l'origine d'un épisode court est plus tardive que celle d'une inertie unique de même temps de séjour : le retard de l'émetteur publié est alors surestimé, d'environ 1,3 fois sur les formes simulées, ou n'est jamais publié lorsque l'épisode reste dans le régime transitoire.

L'ordonnée à l'origine fixe le retard de l'émetteur, pas la forme de la réponse. Le modèle de puissance délivrée le répartit en un temps mort pur $L = \min(\text{temps mort actif}, T_{ar})$, puis en une inertie du premier ordre de $T_{ar} - L$. Le temps mort est détecté après le début de la réponse ; il borne donc le temps mort pur par le haut : le modèle ne délivre jamais de puissance plus tôt qu'un temps mort pur de même ordonnée à l'origine. Sans retard de l'émetteur publié ni temps mort fiable, la puissance délivrée est égale à la puissance appliquée. Pour une fenêtre active qui débute $t_0$ après l'échelon et dure $W$, la fraction délivrée moyenne vaut, avec $\tau = T_{ar} - L$ :

$$ \eta = \frac{1}{W}\int_{t_0}^{t_0+W} \left(1 - e^{-(t-L)/\tau}\right)\mathbb{1}_{t>L}\,dt $$

Le modèle est résolu dès que le retard de l'émetteur est publié, ou après `RESPONSE_LAG_RESOLVE_EPISODES = 4` phases de chauffe sans publication (la puissance appliquée est alors utilisée). Une phase de chauffe regroupe les épisodes actifs séparés par des coupures de puissance inférieures à `RESPONSE_LAG_PHASE_MIN_OFF_S = 600 s` : un épisode qui commence moins de 600 s après la fin du précédent ne compte pas comme une phase supplémentaire. Jusque-là, les mesures de `a` sont collectées sans publication. Lorsque le retard de l'émetteur publié apparaît ou varie de plus de `RESPONSE_LAG_MODEL_CHANGE_REL = 0.5` avant la première publication de `a`, les mesures collectées sont recalculées avec la nouvelle puissance délivrée. Seules les fenêtres admises par la porte de stabilisation sont recalculées ; les autres sont écartées, puisque leur correction dépend de la forme de la réponse.

L'état est publié dans `analysis.learning.response_lag` (`lag_s`, `estimates`, `episodes`, `episodes_target`, `episode_median_s`, `last_reason`) et persisté avec l'état d'apprentissage. `episodes` compte les phases de chauffe terminées depuis le début de l'apprentissage, y compris après la publication du retard de l'émetteur ; `episodes_target` vaut `RESPONSE_LAG_RESOLVE_EPISODES`. Lorsque les deux collectes ont atteint leur seuil et que `a` attend ce modèle, `learning.bootstrap_status` vaut `step2 - waiting response lag: estimates N, heating phases K/4`.

Le retard de l'émetteur est appris sur des épisodes à puissance constante, que fournit le bootstrap par hystérésis. En régulation PWM, le rapport cyclique change d'un cycle à l'autre : une installation déjà sortie du bootstrap en fournit donc rarement.

#### Bilan de chauffe

`HeatingBalance` (`heating_balance.py`) mesure, sur chaque phase de chauffe suivie d'un arrêt, la vitesse de chauffe `a` (°C/min à pleine puissance) et le retard de l'émetteur à partir d'un bilan énergétique de la pièce. Il est publié ; la régulation ne lit que la médiane des derniers retards de l'émetteur qu'il a mesurés, comme horizon de l'anticipation (voir la section 4.3). Il ne fonctionne qu'en mode chauffage, à partir des événements de chauffe du détecteur d'événements de mesure (3.7, Événements de mesure et valeurs de départ).

Une phase commence au premier intervalle où la puissance dépasse 1 % et se termine après sa coupure. Les chauffes séparées de moins de `HEATING_BALANCE_PHASE_MIN_OFF_S` (10 minutes) sans chauffage appartiennent à la même phase. Avant la phase, le repos (chauffage éteint) doit durer au moins `HEATING_BALANCE_MIN_REST_S = 1800 s`. Une référence, la droite des moindres carrés des échantillons de température sur la fin du repos (au plus `HEATING_BALANCE_BASELINE_SPAN_S = 3600 s`, au moins 4 échantillons), tient lieu de trajectoire passive : $T_0(t) = T_0 + s\,(t - t_{start})$. Pendant la phase, l'estimateur cumule le temps à pleine puissance $t_{on}$ (somme de la puissance × intervalle) et l'aire $A$ de $T - T_0(t)$ en °C·min.

Après la coupure, il cherche le pic de température : les échantillons sont regroupés en tranches consécutives de `HEATING_BALANCE_PEAK_BIN_S = 300 s` à partir de la coupure, et la première tranche à la moyenne strictement la plus élevée est le pic, confirmé lorsqu'une tranche fermée se trouve au moins `HEATING_BALANCE_PEAK_CONFIRM_S = 1800 s` après elle. La phase se termine sans mesure lorsque la chauffe reprend alors que la meilleure tranche est la dernière à contenir des échantillons, ou lorsque `HEATING_BALANCE_PEAK_MAX_S = 4 h` s'écoulent sans pic confirmé. Avec $\Delta_p = T_{peak} - T_0(t_{peak})$, $A_p$ l'aire au pic, $b$ le taux de perte publié (0 lorsqu'il est inconnu) et $t_{on}$ en minutes :

$$a = \frac{\Delta_p + b\,A_p}{t_{on}}$$

La mesure est retenue lorsque $t_{on} \ge$ `HEATING_BALANCE_MIN_ON_S = 600 s` et $\Delta_p \ge$ `HEATING_BALANCE_MIN_RISE_C = 0.3 °C`. Les `HEATING_BALANCE_HISTORY = 7` dernières mesures sont conservées ; les valeurs publiées de `a` et du retard de l'émetteur sont leurs médianes.

Les retards de l'émetteur sont aussi conservés dans un historique propre, de même longueur : une mesure qui ne donne pas de retard de l'émetteur (puissance non constante, non résolu) n'en retire aucun, contrairement aux mesures retenues. La médiane de cet historique est le retard de l'émetteur retenu, publié comme `kept_lag_s` (`kept_lags` en est le nombre). Un état sauvegardé qui contient un retard de l'émetteur publié mais aucun retard retenu le conserve comme unique valeur retenue.

Pour une phase à puissance constante (sa puissance moyenne $u_0 = t_{on}/D$ vaut au moins `1 - RESPONSE_LAG_U_TOL` fois sa puissance la plus haute par intervalle), la chaleur encore manquante à la coupure donne le retard de l'émetteur. Avec $\Delta_c = T_{cut} - T_0(t_{cut})$ ($T_{cut}$ est la moyenne des échantillons à ±150 s de la coupure), $A_c$ l'aire à la coupure et $D$ la durée de la phase, le déficit de livraison vaut $\delta = \left(t_{on} - 60\,(\Delta_c + b\,A_c)/a\right)/u_0$ en secondes. Un émetteur du premier ordre de constante de temps $\tau$ donne $\delta = \tau\,(1 - e^{-D/\tau})$, résolue en $\tau$ par dichotomie ; le retard de l'émetteur vaut 0 lorsque $\delta \le 0$ et est inconnu lorsque $\delta \ge 0.98\,D$.

Sur une telle phase, la chaleur que l'émetteur contient encore au pic est comptée elle aussi. Au pic la température est plate : l'émetteur donne donc encore ce que la pièce perd, $p = \max(0,\ b\,\Delta_p - 60\,s)$ en °C/min, avec $s$ en °C/s. Cette puissance est supposée arriver sur un retard de l'émetteur, ce qui ajoute $p\,\tau/60$ à la chaleur de la phase :

$$a(\tau) = \frac{\Delta_p + b\,A_p + p\,\tau/60}{t_{on}}$$

La vitesse de chauffe et le retard de l'émetteur sont résolus ensemble : en partant de $\tau = 0$, le déficit calculé avec $a(\tau)$ donne le $\tau$ suivant, jusqu'à ce que deux valeurs successives diffèrent de moins de 1 s, en 60 pas au plus. Le retard de l'émetteur est inconnu lorsque l'itération ne se stabilise pas. La mesure conserve $a(\tau)$ comme vitesse de chauffe. Sans ce terme, le retard de l'émetteur est lu trop court dans une pièce dont les murs absorbent la chaleur, où le pic survient alors que l'émetteur est encore chaud.

L'état est publié dans `analysis.learning.heating_balance` et persisté avec l'état d'apprentissage. Les mesures sont restaurées après un redémarrage ; la phase en cours reprend après un redémarrage ponté et est abandonnée sinon. Les retards de l'émetteur retenus sont restaurés également.

#### Événements de mesure et valeurs de départ

Un détecteur unique (`measure_event.py`, `MeasureEventDetector`) surveille la puissance
appliquée à l'émetteur et la température de la pièce, en chauffage et en refroidissement.
Un événement est un repos, une phase de puissance supérieure à 1 % (les coupures plus courtes
que `HEATING_BALANCE_PHASE_MIN_OFF_S` appartiennent à la phase) et une coupure, puis la
recherche du pic de température. La recherche du pic (`peak_search.py`) est celle décrite pour
le bilan de chauffe : tranches de 300 s, première moyenne strictement la plus élevée (la plus
basse en refroidissement), confirmée par une tranche fermée 1800 s plus tard, abandonnée après
4 heures. Chaque événement porte la source qui commandait au début de sa phase : impulsion de
mesure, hystérésis du bootstrap, impulsion d'une calibration ou régulation. Le bilan de chauffe lit
les événements de chauffe quelle que soit leur source ; le lecteur ci-dessous lit ceux du bootstrap et
des impulsions.

Jusqu'à ce que des valeurs soient retenues, pendant la phase d'apprentissage, une calibration ou une lecture, un
enregistrement borné (`MeasureRecord`) conserve la puissance appliquée, la température de la pièce
et la température extérieure sous forme de points de changement : 6000 points au plus par canal et
environ 30,5 heures. Pour un contacteur, le canal de puissance contient les vrais fronts de marche et
d'arrêt de chaque cycle que l'ordonnanceur a appliqué (d'après les accusés de commande appliquée) ;
pour une vanne, ou un ordonnanceur sans accusés, il contient la fraction de cycle. L'enregistrement
est sauvegardé.

Un événement fermé du bootstrap ou d'une impulsion de mesure ouvre une lecture (`event_reading.py`). Sa fenêtre commence
1800 s avant l'échelon de chauffe et se termine d'abord à la fermeture de l'événement : elle contient
donc les échantillons qu'attendait la confirmation du pic. Un ajustement (`event_fit.py`) explique
ensuite la température enregistrée avec toute la chaîne : l'émetteur donne à la pièce la puissance
appliquée retardée d'un retard pur $D$ et filtrée par un retard du premier ordre $\tau$, et la pièce
suit $dT/dt = a\,q - b\,(T - T_{out})$. Il trouve $D$, $\tau$ et $b$ par recherche et $a$ par
moindres carrés ; le retard de l'émetteur de la lecture vaut $D + \tau$. L'ajustement s'exécute en
arrière-plan, hors de la régulation : c'est l'un des travaux de la boucle d'arrière-plan unique qui
exécute aussi l'apprentissage du retard de l'émetteur et le modèle de pièce, un travail à la fois, et
le calcul suivant en applique le résultat. Après chaque ajustement, la fenêtre est prolongée à six
retards du résultat, au plus 8 ajustements, et la lecture est complète lorsque la fenêtre en couvre 90 %.
numpy et scipy sont chargés au premier ajustement.

Deux lectures complètes du même mode concordent lorsque leur `a` et leur retard de l'émetteur diffèrent
chacun d'au plus 20 % du plus grand. Les moyennes de deux lectures concordantes sont retenues pour leur
mode, sauvegardées, et effacées par une réinitialisation de l'apprentissage. Les valeurs retenues sont par
mode, comme le canal `a` de l'estimateur : une période dans l'autre mode démarre sans valeurs retenues et
fait ses propres lectures et impulsions. Un arrêt du thermostat, un changement de mode ou le début d'une
calibration termine les lectures en cours.

**Valeurs de départ.** Le `a` retenu de chaque mode est donné une seule fois à l'apprentissage de `a` et
`b` comme valeur de départ, et le `b` retenu (propriété de la pièce, partagé par les modes) avec la
première transmission seulement : avec le démarrage direct à la rétention, avec le démarrage par amorçage
à la fin du bootstrap. Elles tiennent lieu des valeurs propres de l'apprentissage et n'écrivent aucun
historique de mesures : `a` cesse de tenir lieu à la première publication de `a` par l'apprentissage
lui-même, et `b` lorsque la constante de temps propre de l'apprentissage devient fiable. Un changement de
mode HVAC met de côté le `a` en place avec son canal et le restitue avec lui. Les valeurs de la première
transmission rapportent 5 mesures de crédit de montée en charge du feedforward (4.2) par valeur donnée,
conservé lorsqu'elle est relâchée ; une transmission ultérieure de l'autre mode n'en rapporte aucune.
Avec le démarrage par amorçage, le retard de l'émetteur des lectures est publié avec les valeurs ; il ne
fixe ni l'horizon de l'anticipation ni le retard de l'émetteur des phases à puissance constante.

**Démarrage direct.** Le PI commande dès le premier calcul, sur les gains de repli. Jusqu'à
`PULSE_MAX_COUNT = 3` impulsions de mesure par mode (`measure_pulse.py`) remplacent la commande du PI
pour une courte séquence, tant que l'étape d'apprentissage dure et que les lectures n'ont retenu aucune
valeur dans le mode. Une impulsion démarre lorsque la pièce est à la consigne ou au plus
`PULSE_START_UNDER_C = 0,3 °C` du côté de la demande, que la commande précédente est inférieure à
`PULSE_START_MAX_COMMAND = 0,8` et que `PULSE_MIN_GAP_S = 6 h` se sont écoulées depuis l'impulsion
précédente. Elle se repose (au plus `PULSE_REST_MAX_S = 45 min`, ou jusqu'à ce que la pièce soit à
`PULSE_REST_UNDER_C = 0,5 °C` du côté de la demande), chauffe à pleine puissance jusqu'à ce que
l'estimateur de temps mort ait détecté la réponse, qu'elle se détache de trois pas de la sonde et que la
pièce soit revenue à moins de `PULSE_HEAT_BACK_C = 0,1 °C` du côté de la demande de la consigne, cette
dernière condition seulement tant que l'étape a duré moins de `PULSE_HEAT_EXTEND_MAX_S = 30 min` (au plus
`PULSE_HEAT_MAX_S = 2 h`, ou dès que la pièce est à `PULSE_HEAT_OVER_C = 0,3 °C` de l'autre côté), puis
s'arrête jusqu'à la confirmation du pic de température (au plus `PULSE_STOP_MAX_S = 4 h` ; le PI reprend la
main lorsque la pièce est à `PULSE_TAKEOVER_C = 0,5 °C` du côté de la demande et plus basse d'un pas de la
sonde qu'au début de l'arrêt, et dans tous les cas à `PULSE_TAKEOVER_CAP_C = 0,8 °C`). Une impulsion interrompue
par un redémarrage, un arrêt, un changement de consigne ou de mode n'est pas reprise et
compte. Les lectures des événements que provoquent les impulsions donnent les valeurs retenues. Tant que
l'apprentissage n'a pas publié son propre modèle, elles en tiennent lieu : `pre_model_gains` donne les
gains à partir du `a` retenu et du temps mort actif (la règle IMC/SIMC d'une pièce lente, aucun gain
au-dessus de sa valeur de repli), le retard de l'émetteur retenu est l'horizon de l'anticipation (source
`measurement pulses`) et le retard de l'émetteur de l'estimateur de retard de réponse, qui compte alors
comme résolu. Le terme intégral repart de zéro au calcul où l'étape d'apprentissage se termine.
L'estimateur de temps mort lit le temps mort après un arrêt sur le pic de température, avec la
recherche de pic des événements de mesure et une confirmation plus précoce : le pic est aussi confirmé à
la fermeture d'une tranche postérieure à la meilleure dont la moyenne est inférieure à la meilleure d'au
moins le plus grand de `STOP_PEAK_DROP_MIN_C = 0,05 °C`, `STOP_PEAK_DROP_STEPS = 2` pas de la sonde (la
plus petite variation de la dernière heure, au moins 0,01 °C) et `STOP_PEAK_DROP_RISE_FRAC = 0,5` fois la
montée entre la coupure et la meilleure tranche ; quand la chauffe reprend, la tranche en cours est fermée
et soumise au même test, sinon l'arrêt ne donne pas de valeur. La règle des 1800 s est la confirmation la
plus tardive. Seuls les arrêts qui suivent un cycle à pleine puissance
alimentent la valeur publiée.

**Calibration.** Sur demande (`force_smartpi_calibration`), une fois le premier modèle publié, une
calibration (`calibration.py`) lance des impulsions de mesure quel que soit le démarrage, avec son propre
compte (`PULSE_MAX_COUNT = 3` impulsions au plus) ; elles ne comptent pas pour l'étape d'apprentissage.
`Calibration.request` répond à chaque demande sans lever d'erreur et publie la réponse (`last_request`) :
refusée pendant l'étape d'apprentissage, avec l'apprentissage en pause, avec le thermostat éteint par
l'utilisateur (le dernier calcul a vu le mode HVAC OFF sans la raison fenêtre), et moins de
`CALIBRATION_MIN_INTERVAL_S = 24 h` après la fin de la dernière calibration non annulée ; ignorée pendant une
calibration en cours ; une fenêtre, une coupure de garde ou un délestage diffèrent seulement les impulsions.
Les impulsions d'une calibration ont leurs propres limites, calculées à chaque calcul par `pulse_limits` à
partir des temps morts fiables du mode (`La` actif, `Lp` passif, 0 s'ils ne sont pas fiables), de l'horizon
d'anticipation en usage `H` et de la vitesse de baisse `s` (moindres carrés sur les dernières
`CALIBRATION_FALL_WINDOW_S = 900 s` de la température brute, du côté de la demande, au moins 0) ; les
limites statiques sont figées au début de l'impulsion :

| Étape | Impulsion d'une calibration |
|---|---|
| démarrage | demande dans [-`CALIBRATION_START_OVER_C` = -0,2 °C, `PULSE_START_UNDER_C` = 0,3 °C], commande précédente < 0,8, `clamp(2 H, 1 h, 6 h)` après la fin de l'impulsion précédente |
| repos | se termine à `min(Lp + CALIBRATION_REST_EXTRA_S, 4 h)` (`CALIBRATION_REST_EXTRA_S` = 900 s de fenêtre libre + 300 s), ou quand `demande + s La >= PULSE_REST_UNDER_C` = 0,5 °C |
| chauffe | montée vue et (`demande <= PULSE_HEAT_BACK_C` = 0,1 °C ou étape >= `PULSE_HEAT_EXTEND_MAX_S` = 30 min), 0,3 °C au-dessus, ou `clamp(2 (La + H), 2 h, 4 h)` |
| arrêt | événement fermé et réponse à l'arrêt non attendue ; reprise à `demande >= 0,5 °C` une fois la pièce plus basse d'un pas du capteur qu'au début de l'arrêt, ou à `demande >= CALIBRATION_DEPTH_CAP_C` = 0,8 °C ; 4 h |

Les impulsions de l'étape d'apprentissage gardent les constantes de `measure_pulse.py`. Une impulsion est
appliquée comme une commande tout-ou-rien : engagée aussitôt, puis l'estimateur de temps mort est alimenté
avec la puissance engagée, une fois par calcul ; ses mesures rejoignent la médiane des temps morts comme
toute autre. La phase est `Calibration` tant qu'une impulsion de la calibration commande la sortie ; entre
deux impulsions, la régulation fonctionne en phase stable. Les événements des impulsions de calibration
(source `calibration`) sont lus comme les autres mais forment un ensemble à part, comparés entre eux
seulement : deux lectures concordantes (`a` et retard à 20 % près) donnent la lecture de la calibration
(`a`, `b`, retard de l'émetteur, et si les deux `b` concordent à 20 % près). À la fin sur cette lecture,
`LearningStart._correct` compare chaque grandeur à la valeur en vigueur dans le mode de la calibration
(`|a|`, `b`, l'horizon d'anticipation en usage) et la corrige quand `|lecture - en vigueur| >
CALIBRATION_CORRECT_REL * en vigueur` (0,30) :

- `a` : `ABEstimator.correct_a` réinitialise le canal du mode et pose la lecture comme valeur de départ
  (`StartingValues.correct`), libérée à la première publication propre de `a` ;
- `b` : lu et publié seulement (`CALIBRATION_CORRECT_B = False` : le `b` lu sur une impulsion est biaisé
  dans une pièce à murs épais). Avec l'interrupteur activé et les deux `b` concordants,
  `ABEstimator.correct_b` vide le canal de `b` (mesures, historique publié, compteur, dérive, `own_b`) et pose
  la lecture, libérée quand la constante de temps propre est de nouveau fiable ; les mesures de `a` sont
  rejouées avec elle ;
- retard (démarrage direct seulement) : gardé dans `calibration.lag_s` par mode et donné à l'estimateur de
  retard de réponse à la place du retard retenu au démarrage, à chaque calcul ; il n'entre jamais dans
  l'horizon d'anticipation.

Avec `CALIBRATION_SKIP_WHEN_OWN_AGREE = True` (désactivé par défaut), `a` ou `b` n'est pas corrigé tant que
son tampon de dérive est vide et que ses `CALIBRATION_OWN_RECENT = 3` dernières mesures sont dans le seuil
de la valeur en vigueur. Une correction verrouille le crédit du feed-forward au compte de la rampe d'avant
(`StartingValues.latch`) et note le gain appris `b / a` d'avant dans le transfert de maintien
(`HoldingTransfer.note_learned_step`) : au feed-forward suivant, le saut de la puissance de maintien apprise,
évalué à la même consigne, la même température extérieure et la même montée en charge, est reporté dans
l'intégrale. Un changement de `Kp` qui suit une correction n'est pas reporté
dans l'intégrale : le terme proportionnel agit sur l'erreur comme d'habitude, et un changement de `Ki` ne fait que
remettre l'intégrale à l'échelle (`SmartPIController.transfer_gains`). Un démarrage par bootstrap dont `b` est posé par une correction ne revient pas à son étape
d'apprentissage. Le bilan de chauffe et l'estimateur de retard de réponse observent les impulsions comme
toute phase de chauffe. Une calibration se termine lorsque deux de ses lectures concordent (`readings
agree`), lorsque ses impulsions sont faites et leurs lectures terminées (`pulses done`), ou
`CALIBRATION_MAX_S = 48 h` après son premier calcul (`time limit`) ; ses lectures en cours se terminent
alors. Lorsque cette fin arrive pendant une impulsion de la calibration, l'impulsion se termine d'abord : au
repos elle rend la main aussitôt, dans son étape de chauffe elle passe à son étape d'arrêt
(`MeasurePulse.stop_heating`, raison `calibration ended`), dans son étape d'arrêt elle continue ; la
calibration se termine au calcul où l'impulsion se termine, avant que le PI calcule, et les corrections
s'appliquent à ce calcul. La fin est aussi vérifiée sur un calcul coupé par la garde
(`LearningStart.follow_calibration`). Pendant une calibration (de la demande à la fin), `ABEstimator.learn`
ne prend aucune mesure de `a` : une fenêtre avec `u > U_ON_MIN` est écartée (`skip: calibration in
progress`) ; les fenêtres de `b`, les temps morts, le bilan de chauffe et le retard de réponse continuent.
Après chaque impulsion de calibration (`LearningStart.recovering`), l'intégrale est figée (`I:HOLD`, source
`calibration_recovery`) jusqu'à ce que la pièce atteigne la consigne ; les impulsions de l'étape
d'apprentissage ne la figent pas. Un arrêt du thermostat termine l'impulsion en cours, qui compte, et la calibration continue ; un changement de
mode HVAC, un retour de l'étape d'apprentissage, une réinitialisation ou une mise en pause de
l'apprentissage l'annule (`cancelled`) et termine aussitôt son impulsion. La demande, les impulsions faites, le mode, le début, la dernière
demande, les valeurs en vigueur, les corrections et le retard corrigé sont sauvegardés (`calibration`) ; les
valeurs de départ corrigées sont sauvegardées avec `corrected` dans `starting_values` et se chargent même
sans valeurs transmises ; un état sauvegardé avec les anciens blocs de calibration (`cal_state`,
`ac_state`) se charge sans calibration en cours.

L'enregistrement, les lectures en cours, les valeurs retenues et les impulsions démarrées sont sauvegardés
(`measure_events`, `event_reading`, `measure_pulse` et `learning_start`, et `starting_values` dans
`est_state`) ; un état sauvegardé sans eux se charge. Après un redémarrage ponté, les lectures reprennent
(un ajustement en cours est remis en file) ; sinon les lectures en cours se terminent et l'enregistrement
est abandonné.

#### Modèle de réponse (publié seulement)

`response_model.py` tient, par mode, un modèle dynamique de la réponse de la pièce à la
puissance : un temps mort pur $\theta$ (sonde, transport, actionneur) suivi d'une
constante de temps de réponse $\tau_e$ du premier ordre (l'émetteur, l'air et la masse
proche ensemble). Rien dans la régulation ne
le lit ; il est publié à côté des temps morts et des retards ci-dessus.

Un second lecteur (`ModelReader`, `event_reading.py`) lit les événements de mesure avec
les mêmes fenêtres que le lecteur ci-dessus (avance de 1800 s, première fin à la clôture
de l'événement, prolongée à six retards de la dernière estimation, 8 ajustements, 24 h),
sur un enregistrement propre qui tourne à chaque étape et avec son propre travail
d'arrière-plan. Il lit les événements des impulsions, du bootstrap et des calibrations
quelles que soient les valeurs retenues, et les événements de régulation qui suivent un
repos de 1800 s, une chauffe d'au moins 600 s en équivalent pleine puissance, une hausse
d'au moins 0.3 °C jusqu'à leur pic, alors que SmartPI pilote l'actionneur sur toute la
fenêtre (ni arrêt, ni exclusion vue par le thermostat : fenêtre, délestage, mode sécurité,
commande remplacée) et seulement avec une position home connue. La fenêtre d'un événement
de régulation se termine six retards d'émetteur après le début de sa chauffe (le retard
publié ; sans modèle publié, le retard de sa dernière estimation, une fenêtre plus longue
étant ajustée de nouveau plus courte), quelle que soit la clôture de la phase régulée.

Avec une position home connue, la fenêtre d'un événement dont la chauffe démarre dans
l'obscurité (`quiet_periods/solar.py`) se termine au lever du soleil suivant. Un événement
est de jour lorsque sa chauffe ne démarre pas dans l'obscurité, ou lorsque l'obscurité
qui suit le début de la chauffe dure moins de trois retards d'émetteur (le retard publié,
sinon celui de son estimation). Un événement de jour, de
toute source, est ajusté et montré avec `daytime` ; il n'est donné au modèle que lorsque
le mode n'a encore aucun événement, et le premier événement dans l'obscurité le remplace
alors. Sans position home aucun événement n'est de jour (et aucun événement de régulation
n'est lu).

L'ajustement (`event_fit.fit_response`) explique la pièce avec la chaîne du lecteur
ci-dessus, avec des résidus seulement aux franchissements de niveau de la pièce : un
changement d'un pas de sonde (au plus 1.5 pas) est pris au milieu des deux niveaux, un
changement plus grand à sa nouvelle valeur, et le modèle est interpolé à ces instants.
Une grille de profils de $\theta$ (0 à 1200 s par pas de 60 s, puis 1500 à 5400 s par pas
de 300 s) et de $\tau_e$ (24 valeurs sur échelle logarithmique, de 60 s à 4 h) cherche $b$
par une recherche scalaire bornée pour chaque cellule ($a$ et la température de départ par
moindres carrés), puis Nelder-Mead affine la meilleure cellule dans les bornes de la
grille. Les résidus de franchissements successifs sont corrélés sur environ
$S = \theta + \tau_e$ : le nombre de résidus indépendants est pris égal à
$n = \max(W/S, 1)$, $W$ étant la fenêtre après le début de la chauffe. Les cellules dont
le coût est au plus le minimum multiplié par $1 + 3.84 / n$ donnent les plages de $\theta$
et de $S$ ; l'ajustement distingue $\theta$ lorsque la plage de $\theta$ est au plus
$\max(300\,s, 0.5\,S)$. Un ajustement est rejeté avec moins de 8 franchissements, une
hausse de moins de 3 pas de sonde, aucune chauffe vue ($a = 0$), $\theta$ ou $\tau_e$ sur
sa borne haute (5400 s, 4 h), ou une racine de la moyenne des carrés des résidus de plus de
2 pas de sonde (0.01 °C au moins) : `fit too far from the room temperature`. Un ajustement
dont $\tau_e$ est sur sa borne basse (60 s, un émetteur rapide dont la vraie valeur peut
être plus courte) est accepté à 60 s et montré comme `accepted, response time constant at
its lower limit`.

Par mode, les 7 derniers événements acceptés sont conservés. $S$ est leur médiane, $\theta$
la médiane des événements qui le distinguent (au plus $S$), sinon $S/2$ (le milieu de
$[0, S]$ : temps mort effectif $3S/4$) ; $\tau_e = S - \theta$. Un événement publie le modèle en `provisional`, deux événements dont les $S$
concordent à 20 % près le rendent `measured`, et aucun événement qui distingue $\theta$
le rend `dead time assumed`. Les événements d'une calibration rejoignent l'historique ;
lorsque deux d'entre eux concordent sur $S$ et que leur moyenne s'écarte de plus de 30 %
du $S$ de l'historique, ces deux-là le remplacent. Grandeurs dérivées : temps mort
effectif $\theta + \tau_e/2$, retard de l'émetteur $\theta + \tau_e$, temps de
stabilisation $\theta + 3\tau_e$. Chaque pic confirmé après une coupure donne aussi un
échantillon $\tau_e = (t_{peak} - \theta) / \ln(1 + s_{cut}/\ell)$, avec $\ell = b\,(T_{cut}
- T_{out})$ et $s_{cut}$ la pente aux moindres carrés de la pièce sur les dernières
$\min(\tau_e, 1800\,s)$ avant la coupure, lorsque $s_{cut} \ge \ell$ et que le pic vient au
moins 600 s après $\theta$ ; les échantillons sont publiés seulement.

Lorsque la séparation est incertaine (`dead time assumed`), chaque utilisateur du modèle
prend pour son propre usage l'extrémité prudente de $\theta \in [0, S]$ : les gains lisent
le temps mort effectif $3S/4$ (plus la demi-période d'échantillonnage $h/2$ là où les gains
en tiennent compte), l'horizon d'anticipation lit $S$, et l'attente d'une fenêtre de `b`
après un arrêt vaut $S \cdot \max(3, \ln(q_{stop} / 0.05\,\ell))$, avec $q_{stop}$ la chaleur
encore à venir à l'arrêt et $\ell$ les pertes (le cas $\theta = 0$, dont la traîne
d'émetteur est la plus longue).

Le modèle, ses lectures et son enregistrement sont sauvegardés sous `response_state`. Un
état sauvegardé sans `response_state` est amorcé à partir du temps mort actif $L$ de
chaque mode (`dt_est_state`) et du premier retard de l'émetteur $S$ parmi le retard
retenu des lectures, le retard retenu du bilan de chauffe (en chauffage) et le retard de
réponse : $\theta = \mathrm{clamp}(2L - S, 0, L)$, ou $2L/3$ sans $S$ (la séparation en
moitiés de $S = 4L/3$), et $\tau_e = 2(L - \theta)$, de sorte que le temps mort effectif égale $L$. L'amorçage est
publié en `previous version`, `provisional`, jusqu'au premier événement accepté du mode ;
un mode sans $L$ fiable n'a pas d'amorçage.

### 3.8 Périodes calmes de l'apprentissage du retard de l'émetteur

L'apprentissage du retard de l'émetteur (`smartpi/tar_learning/`) et le modèle de pièce sélectionnent leurs données avec `smartpi/quiet_periods/`, un module Python pur partagé par les méthodes qui ont besoin d'une pièce à l'abri des apports solaires. Aucune heure locale n'intervient dans la sélection ; les temps sont des secondes epoch et des intervalles semi-ouverts `[start, end)`.

**Obscurité.** La position provient de la configuration de Home Assistant (`hass.config.latitude`, `hass.config.longitude`), injectée par le handler et validée par le manager : nombres finis dans [-90, 90] et [-180, 180] (zéro est valide). L'élévation géométrique du centre du soleil est calculée avec le paquet `astral` livré avec Home Assistant. L'obscurité correspond à un centre situé sous `-0.833 - 0.5 = -1.333` degrés : -0,833 degré est le lever et le coucher du soleil standard (le bord supérieur sur l'horizon apparent, 34' de réfraction plus 16' de rayon), et 0,5 degré couvre l'étalement habituel de la réfraction près de l'horizon. L'élévation est échantillonnée toutes les 600 s sur une grille alignée sur l'epoch ; un échantillon ne compte comme sombre qu'à 0,05 degré sous le seuil (l'élévation s'écarte d'une corde d'au plus 0,014 degré sur un pas de 600 s près de l'horizon), chaque franchissement est affiné par dichotomie à 1 s, puis chaque borne est repoussée de 120 s vers l'intérieur pour la précision de l'algorithme solaire. L'obscurité se termine donc quelques minutes avant le lever du soleil et commence quelques minutes après le coucher, jamais plus tard que le lever. Une obscurité de plus de 24 h (nuit polaire) est découpée en blocs de 24 h ancrés sur son début réel ; un jour polaire n'a pas d'obscurité. Sans position valide (ou sans `astral`), il n'y a ni obscurité ni période : les méthodes restent silencieuses et `quiet_periods.reason` en donne la raison.

**Périodes.** Un plateau est un intervalle maximal à consigne constante. Une séquence de baisse de consigne va de la baisse à la hausse suivante (les baisses suivantes restent dans la séquence). Une hausse vers une consigne encore inférieure à celle d'avant la baisse, avec la pièce strictement au-dessus de la nouvelle consigne au moment de la hausse, ne demande pas de chauffe : elle ne termine pas la séquence, et son plateau en est un morceau de plus. Une obscurité contient au plus un épisode : la plus longue partie d'une séquence de baisse de consigne située dans l'obscurité, ou toute l'obscurité lorsque la consigne ne change pas à l'intérieur (consigne maintenue). L'épisode se termine à la hausse qui achève la séquence, ou à la fin de l'obscurité. Ses composantes calmes sont ses morceaux de plateau intersectés avec les plages valides de l'historique (`TarHistory.valid_spans` : tout sauf les intervalles invalides) ; un redémarrage ponté ne laisse aucun intervalle invalide et ne les coupe pas. La composante retenue est la plus longue (au moins 1 h, la plus ancienne en cas d'égalité).

**Début régulé.** Pour une composante, le premier démarrage du chauffage à partir du début de son plateau avec la température intérieure à moins de 0,3 °C de la consigne ; lorsqu'il précède la composante, le début de la composante si les données entre les deux sont valides. Un démarrage du chauffage dans les 60 s suivant un redémarrage ne compte pas. Aucun lorsque la pièce ne régule jamais à cet endroit.

**Intervalle d'identification.** La composante retenue, prolongée après la hausse qui termine l'épisode tant qu'il fait encore nuit, que la nouvelle consigne reste constante et que les données sont valides, pendant 2 h au plus (l'horizon de réponse de l'ajustement d'une montée de confort). La réponse à la hausse est la principale excitation de l'émetteur : sans elle, la constante de temps de l'émetteur d'une pièce régulée est mal déterminée. La nuit régulée et la montée de confort s'arrêtent toujours à la hausse.

**Clés.** Un épisode est fermé dès que sa fin est antérieure ou égale à la dernière observation, établi dès que son intervalle d'identification est connu lui aussi, et complet lorsque l'historique le couvre depuis son début. Sa clé est `q<version>:<origine>:<début de la composante retenue>` à 0,1 s près, l'origine étant le début de l'épisode. Le manager reconstruit le catalogue au plus toutes les 10 minutes et conserve les épisodes établis et complets (60 au plus) avec leurs composantes : le catalogue conservé n'avance que vers l'avant, si bien qu'une clé ne change jamais lorsque l'historique est élagué, et une période retrouvée reçoit la même clé.

**Usages.**

| Règle | Données |
|---|---|
| Montée de confort | une hausse d'au moins 1 °C après une consigne basse maintenue 2 h (dans l'historique), vers une consigne pas inférieure à la dernière température intérieure, hors des 300 s suivant un redémarrage ; évaluée lorsqu'elle termine un épisode et que la composante qui se termine à la hausse couvre les 2 h qui la précèdent |
| Fenêtre d'ajustement d'une montée | chaque fenêtre de réponse se termine à la fin de l'obscurité ; les règles d'excitation (`exc_window_T`, énergie, hausse, période du capteur) jugent la fenêtre tronquée ; moins de 600 s restantes ne donnent aucun ajustement. Une fenêtre `Wsp` coupée par l'obscurité est une observation censurée : elle ne donne une estimation que si elle dure au moins `TAR_CUT_ESTIMATE_WINDOW_T = 3` fois le plus grand entre la valeur ajustée $T$ et le plancher (la valeur affinée en vigueur, la valeur rapide publiée, la borne inférieure en vigueur et deux fois le temps mort du bootstrap : le premier moment d'un émetteur du premier ordre vaut environ deux fois le temps mort apparent que mesure le bootstrap). Sinon, lorsque les autres règles passent, elle donne la borne inférieure `TAR_CUT_BOUND_FACTOR` $\cdot T = 0.6\,T$ (la marge couvre le bruit d'un ajustement court), confirmée par la même porte qu'une valeur ; une fenêtre coupée qui échoue à une autre règle est refusée comme coupée |
| Indicateur de nuit | périodes passives intersectées avec les composantes calmes de l'épisode de la montée, sur la grille de 600 s du segment |
| G1 / GT4 | plus longue plage sans démarrage du chauffage dans `[début régulé, t_ref)` ; l'absence de début régulé, ou une partie régulée de moins de 1 h 30, est un observable inconnu et laisse la montée non confirmée |
| Nuit régulée | `[début régulé, fin de l'épisode)` de la composante retenue, d'au moins 4 h, valide jusqu'à sa fin ; puis les règles existantes sur les demi-heures, la température, la pente de puissance et le résidu |
| Fenêtre d'identification | les intervalles d'identification de trois épisodes établis consécutifs (tranches entièrement comprises dans celles-ci), l'état des murs commençant 10 h avant le premier ; comparée à la fenêtre des trois épisodes précédents, sans recouvrement |
| Classe de la pièce, valeur rapide | une entrée de nuit et une valeur confirmée par clé, remplacées avant le calcul de la classe de la pièce ou de la publication |
| Modèle de pièce | les mêmes fenêtres de trois épisodes établis et leurs intervalles d'identification, moins les périodes que le suivi a trouvées perturbées (voir 4.2) ; la perturbation du suivi est sommée par clé |

Les évaluations portent leur instant de décision (fin de leur dernier épisode) ; la classe, les paramètres des murs et `τe` en vigueur à une montée sont ceux décidés avant elle. La date locale des diagnostics (`day`) ne sert qu'à l'affichage.

---

## 4. Contrôle Smart-PI

### 4.1 Gains PI

`GainScheduler.calculate()` applique la logique suivante :

1. si `tau`, le signe de `a` ou le temps mort actif ne sont pas fiables,
   repli sur `KP_SAFE = 0.55` et `KI_SAFE = 0.010`,
2. sinon calcul IMC/SIMC avec la magnitude du gain actif :

$$ K_p = \frac{1}{|a| \cdot (\lambda + L)} $$

3. calcul du temps intégral SIMC (Skogestad, 2003)
   $T_i = \min(\tau, 4(\lambda + L))$ (`GAIN_TI_LAMBDA_FACTOR = 4`), puis
   $K_i = K_p / T_i$,
4. bornage des gains ($K_p \in [0.10, 5.0]$, $K_i \in [0.001, 0.050]$) puis application des gels de gouvernance.

Pour une pièce, $\tau$ (de plusieurs heures à plusieurs jours) dépasse largement l'horizon
en boucle fermée $\lambda + L$ : la pièce se comporte presque comme un intégrateur et
$T_i = 4(\lambda + L)$. Avec $T_i = \tau$, $K_i$ tomberait sur le plancher `KI_MIN = 0.001`
et l'intégrale mettrait des jours à corriger une erreur de modèle. L'atténuation de $K_i$
près de la consigne est bornée par ce même plancher.

Le temps intégral effectif $T_i = K_p / K_i$ des gains appliqués est publié en
minutes dans `control.ti_min` (`None` quand $K_i \le 0$).

À chaque changement du $K_i$ appliqué, l'intégrale est multipliée par
$K_{i,old} / K_{i,new}$ : la puissance intégrale $K_i \cdot I$ reste inchangée (transfert
sans à-coup). Le rescale s'applique à tout changement, même minime : l'atténuation près
de la consigne fait varier $K_i$ par petits pas, et un seuil laisserait dériver $K_i \cdot I$
pendant les maintiens.

`GainScheduler.generation` compte les changements des gains du modèle non
atténués (IMC/SIMC ou gains de sécurité). Elle n'avance que tant que les gains
ne sont pas gelés et n'est pas avancée par l'atténuation en bande proche.

Le temps mort actif est `deadtime_heat` en HEAT et `deadtime_cool` en COOL.

Avec l'apprentissage du retard de l'émetteur, le temps mort HEAT $L$ de la règle des gains provient de `tar_learning/policy.applied_value`. Le retard de l'émetteur appris $T$ est la valeur affinée lorsqu'elle est publiée, sinon la valeur rapide, relevée à la borne inférieure en vigueur (la médiane des 5 dernières bornes confirmées) lorsque celle-ci est plus grande. C'est un retard de l'émetteur (un premier moment), environ deux fois le temps mort apparent que mesure le bootstrap : la règle travaille donc avec $L_T = T/2$ (`TAR_GAIN_DEADTIME_FACTOR = 0.5`) face à la référence $L_b = \max(L_{b,0}, L_{boot})$, où $L_{b,0}$ est le premier temps mort de bootstrap fiable vu depuis le début de l'apprentissage (persisté, jamais abaissé) et $L_{boot}$ le courant :

| Cas | $L$ des gains | Autres usages |
|---|---|---|
| pas de valeur apprise | $L_{boot}$ | $L_{boot}$ |
| $L_T < L_b$ | $L_b$ | $L_{boot}$ |
| $L_b \le L_T \le 2 L_b$ (`TAR_APPLY_LONG_FACTOR`) | $L_T$ | $T$ |
| $L_T > 2 L_b$, valeur rapide et valeur de fenêtre concordent, $T$ non borne inférieure | $L_T$ | $T$ |
| $L_T > 2 L_b$ sinon | $2 L_b$ | $L_{boot}$ |

La valeur de fenêtre est la valeur affinée lorsqu'elle est publiée, sinon le `τe` de la dernière évaluation dont les deux fenêtres concordent (un diagnostic lorsque ses nuits ne sont pas régulées). Les deux concordent lorsque $|\ln(T_{fast}/T_{window})| \le \ln 1.25$ (`TAR_AGREE_TOL`). Un $L$ trop court laisse la plage de robustesse recommandée de la boucle, et un $L$ beaucoup trop court rend un émetteur lent instable ; un $L$ trop long coûte de l'erreur intégrée, jamais de la stabilité. Une valeur apprise seule ne raccourcit donc jamais $L$ en dessous de $L_b$, et seule une valeur confirmée par deux méthodes l'allonge au-delà de $2 L_b$. Les autres usages (bande proche, maintien de l'intégrale) n'utilisent $T$ que lorsque les gains utilisent $L_T$.

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

Le compteur de cycles est conservé au redémarrage. L'intégrale restaurée ne
porte que la part de la charge que le feedforward ne couvrait pas : le
feedforward repart donc au niveau qu'il avait avant le redémarrage, sans
à-coup.

Le nombre d'apprentissages valides est le plus grand des mesures admises et du
crédit des valeurs de départ (3.7, Événements de mesure et valeurs de départ) :
chaque valeur de départ donnée compte pour 5 mesures et garde ce crédit lorsqu'elle
est relâchée, si bien que la montée en charge ne recule jamais.

$k_{ff}$ est nul, et donc $u_{ff1}$ l'est aussi, lorsque le modèle n'est pas prêt (`a` non publié avec moins de 5 mesures, ou moins de 5 mesures de `b`), que `tau` n'est pas fiable, ou que le signe de `a` ne correspond pas au mode actif.

$T_{ext}$ est la dernière température extérieure valide reçue par SmartPI. Lorsque la sonde extérieure ne fournit aucune valeur, FF1, le modèle de consigne et la near-band continuent d'utiliser cette dernière valeur, sans limite d'âge. Tant qu'aucune température extérieure n'a été reçue, $u_{ff1}$ est nul (`ff_reason` `ff_no_ext_temp`).

#### Puissance de maintien issue du modèle de pièce

En HEAT, avec des chauffages pilotés par cycles PWM, un modèle de pièce publié remplace
$k_{ff} = b/a$ (`smartpi/room_model/feedforward.py`) dès qu'un modèle statique du bilan
énergétique est en vigueur : la pente $s_{EB}$ (`room_model.energy_balance_loss_per_c`) et
l'apport gratuit constant $g_{EB}$ (`room_model.energy_balance_free_gain_percent` / 100), avec
la partie statique $u_s = s_{EB}(SP - T_{ext}) - g_{EB}$. Avec $\delta_{wall}/a$ la
puissance supplémentaire des murs du suivi (`room_model.wall_extra_power_percent`, avec ses
deux signes) :

| Modèle de pièce | Source (`feedforward.source`) | $u_{ff1}$ |
|---|---|---|
| murs identifiés, suivi `ok` | `room model (air and walls)` | $clamp(u_s + \frac{\delta_{wall}}{a}, 0, 1)$ |
| murs identifiés, suivi non `ok` (en pause, en stabilisation, dérive, ...) | `room model (static part)` | $clamp(u_s, 0, 1)$ |
| modèle simple | `room model (simple)` | $clamp(u_s, 0, 1)$ |
| publié, pas encore de modèle statique en vigueur, ou un modèle dont $s_{EB}$ s'écarte de plus de 5 % du $b_{eq}/a$ publié | `learned model` | la loi apprise ci-dessus, inchangée, sans part des murs |
| rien de publié, COOL, mode vanne | `learned model` | la loi apprise ci-dessus, inchangée |

La partie statique vient du bilan énergétique, jamais du $b_{eq}/a$ ajusté. Avec des murs
lourds, le $b_{eq} = b_{air} - b_{wall}$ ajusté est une petite différence de deux termes plus
grands, et il se déplace avec le retard de l'émetteur utilisé par l'ajustement (d'une
fraction de pourcent par seconde de temps d'émetteur) ; le bilan énergétique sur plusieurs
jours n'en dépend pas. L'ajustement donne la dynamique des murs, $\delta_{wall}/a$. Le modèle
statique seul, sans modèle publié, n'est jamais utilisé : sans la part des murs, il donne une
mauvaise puissance de maintien dans les pièces lourdes, basse après un abaissement de consigne
et haute lorsque les murs restituent de la chaleur. L'apport gratuit fait partie de la partie
statique : sans lui, l'intégrale porterait $g_{EB}$ pour toujours.

**Bilan énergétique.** Une tâche propre calcule, une fois par jour local, 20 minutes après le minuit local et quelles que soient les périodes calmes, le bilan de chaque journée locale entière de l'historique du retard de l'émetteur qui précède le jour (7 au plus ; intervalles de 10 min, intervalles invalides exclus). Minuit n'est que la limite de ces journées civiles (journées de 23 h et de 25 h aux changements d'heure) ; il ne sélectionne aucune période :

$$ u_d = \frac{\sum u \, \Delta t - \Delta T_{in} / a}{\sum \Delta t}, \qquad D_d = \overline{T_{in} - T_{ext}} $$

avec $u$ la puissance appliquée équivalente en chaleur et $\Delta T_{in}$ la variation de
la température intérieure sur chaque segment valide (stockage d'air retiré avec le `a` en
ligne). Une journée est retenue lorsque au plus 20 % de ses intervalles sont invalides, que son
taux de chauffe moyen est d'au moins 0,02 et que $D_d > 0$. Les moindres carrés pondérés (poids :
la part valide du jour) ajustent

$$ u_d = s_{EB} \, D_d - g_{EB} + e_d $$

et la pente seule $u_d = s_0 D_d$. Les jours entiers laissent subsister dans chaque point une
petite part du stockage des murs lié à un changement de consigne ou de température extérieure.
Le verdict utilise les quantiles de Student $t_{0.975}$ sur la dispersion des résidus ; chaque écart-type
est au moins sa valeur par jackknife (un jour retiré à la fois), pour qu'une estimation qui repose
sur un seul jour éloigné des autres ne soit pas retenue :

- au moins 4 jours exploitables, un taux de chauffe moyen d'au moins 0,05 et une moyenne de
  $D$ d'au moins 6 °C ;
- la forme avec apport gratuit exige un écart-type pondéré de $D_d$ d'au moins 1 °C (en
  dessous, les écarts de $D$ d'un jour à l'autre viennent de la régulation elle-même, et la
  pente les lirait comme une perte), une demi-largeur à 95 % $t_{0.975, n-2} \, \sigma_s / s_{EB}
  \le 5$ % (la pente est la référence d'un accord à 5 %), $0 < s_{EB} \le 3$, et un apport qui
  laisse un besoin dans $]0, 1]$ sur chaque jour retenu avec $|g_{EB}| \le s_{EB} \overline{D}$ ;
- avec cette dispersion, une forme avec apport non déterminée est refusée (`free heat gains
  detected; ...` lorsque $g$ est significatif et plausible, sinon `the heating need varies too
  much ...`) : le rapport des moyennes porterait ce que la droite n'explique pas ;
- avec moins de dispersion, la pente seule ($g_{EB} = 0$) avec
  $t_{0.975, n-1} \, \sigma_{s_0} / s_0 \le 5$ % ; un apport que les jours ne peuvent pas montrer
  reste dans $s_0 = s_{EB} - g/\overline{D}$, juste au $D$ habituel seulement ;
- la forme qualifiée ne doit pas dériver : une tendance linéaire de $u_d$ avec le jour, ajustée
  avec le modèle, est refusée lorsqu'elle est significative (95 %) et déplace le besoin de plus
  de 2,5 % de sa moyenne sur l'ensemble des jours (stockage des murs après un changement ; la moitié
  de la tolérance, car le stockage restant après les jours n'est pas dans la tendance).

Un jour refusé garde le modèle qualifié précédent (`room_model.energy_balance_refusal_reason`
indique pourquoi) ; le modèle en vigueur, son jour et son nombre de jours sont sauvegardés avec
le modèle de pièce.

**Recherche de l'émetteur.** Le retard de l'émetteur (son premier moment) des
ajustements est recherché sur une grille fixe, indépendante du temps mort en vigueur : 13
valeurs réparties en logarithme entre 60 s (le pas de l'ajustement) et 3600 s (un tiers du plus
court temps caractéristique des murs $1/\beta$, soit 3 h, afin que l'émetteur et les murs restent
séparables), chacune découpée en une partie du premier ordre (le $\tau_e$ affiné lorsqu'il est connu, plafonné par la valeur) et un temps mort pur, le reste. Le coût $J$ le plus bas est affiné par une recherche de Brent bornée en $\log \tau$ entre
ses deux voisins de grille (à 1 % près, 10 ajustements au plus de plus). Le profil est significatif
lorsque $\max (J - J_{min}) / J_{min} \cdot N_{eff} > \chi^2_{1, 0.95} = 3.84$, avec $N_{eff}$ le
nombre de résidus du meilleur ajustement sur leur temps d'autocorrélation intégré ($1 + 2\sum \rho_k$
jusqu'au premier $\rho_k$ non positif). Un optimum à l'une des extrémités de la grille, avec un
profil significatif, est refusé (`the heating response delay does not match the measured
temperature`). Un profil non significatif ne détermine pas l'émetteur : les ajustements utilisent
l'émetteur en vigueur lorsque l'apprentissage du retard de l'émetteur a publié une valeur affinée, sinon
l'optimum de la grille, et l'accord avec le bilan énergétique juge la perte statique.

**Contrôles des résidus.** Le test de série des résidus de l'ajustement (grille de 10 min dans
les périodes ajustées) compte les changements de signe de leurs moyennes horaires, lorsqu'il y en
a au moins 10 ; les autres contrôles (biais, dispersion, autocorrélation à 1 h, corrélation avec
la sortie de l'émetteur, la consigne et la température extérieure) restent les mêmes.

**Fenêtres d'identification.** Le modèle de pièce lit le catalogue des périodes calmes de l'apprentissage du retard de l'émetteur (voir 3.8) par `TarLearningManager.quiet_periods` ; `room_model/periods.py` en est l'adaptateur. Une fenêtre est formée de trois périodes fermées consécutives du catalogue, chacune ajustée sur son intervalle d'identification (la composante calme, prolongée par la réponse à la hausse pendant 2 h au plus dans l'obscurité), découpée par les intervalles invalides et conservée en morceaux d'au moins 1 h, chaque morceau avec son propre état initial de l'air et des murs. Elle est indexée par sa dernière période et comparée à la fenêtre des trois périodes qui la précèdent, si bien que les deux ne partagent jamais une période ; les premières fenêtres n'en ont pas (`waiting for a second series of quiet periods to confirm`). Une fenêtre est calculée une seule fois, lorsque sa dernière période est établie, de la plus ancienne à la plus récente, au plus une tâche par heure écoulée ; aucune heure locale n'intervient. Une fenêtre dont les données ont quitté l'historique de 7 jours est ignorée.

**Périodes perturbées.** Le suivi conserve la perturbation $d$ de chaque sous-pas suivi de 60 s (36 h au plus) jusqu'à ce que les périodes auxquelles il peut appartenir soient établies, puisque la sélection d'une période n'est définitive qu'alors. Chaque période établie reçoit alors $\int d \, dt$ et la durée suivie sur son intervalle d'identification, un sous-pas franchissant une borne comptant pour sa fraction intérieure ; les pas antérieurs à sa fin sont abandonnés. Une période d'au moins 4 h suivies dont la moyenne s'écarte de plus de 0,08 °C/h de la médiane des 14 dernières périodes est exclue des fenêtres qui la contiennent.

**Information.** Chaque ajustement (modèle simple et modèle avec murs) reçoit la covariance de tous ses paramètres à partir de la jacobienne $J$ de ses sorties pondérées du modèle à l'optimum : les paramètres non linéaires, $a$, les états initiaux de l'air et des murs de chaque morceau et, lorsque la fenêtre a estimé l'émetteur (recherche sur grille, et non la valeur affinée en vigueur), le logarithme du premier moment de l'émetteur (différence centrale à ±2 %) :

$$ C = \tau_{int} \, s^2 (J^\top J)^{-1}, \qquad s^2 = \frac{\sum w r^2}{\sum w - p} $$

avec $\tau_{int}$ le nombre de résidus divisé par leur nombre effectif (le temps d'autocorrélation intégré utilisé par la recherche de l'émetteur) : des résidus corrélés portent moins d'information que leur nombre. Les écarts-types des coordonnées qu'un ajustement apporte s'en déduisent par la méthode delta : $\ln a$ et $\ln(b_{eq}/a)$ pour le modèle simple ; $\ln a$, $\ln \alpha$ et $\ln c$ ($c = \gamma / \beta$) pour le modèle avec murs, dont $b_{eq}/a = (\alpha - c)/a$ n'est jamais la perte statique et est vérifié contre $s_{EB}$ à 5 % près avant une publication et avant tout usage. Un ajustement est refusé (`the quiet periods do not determine the model precisely enough yet`) lorsque $1.96 \, \sigma$ de l'un d'eux dépasse la tolérance avec laquelle l'accord de deux fenêtres le compare : $\ln 1.15$ pour $a$, $\ln 1.25$ pour $b_{eq}/a$ et $\alpha$, $\ln 1.35$ pour $c$. Un modèle avec murs dont la part des murs $c/\alpha$ est inférieure à 0,1 n'est jamais utilisé, si bien que sa précision n'est pas exigée. La covariance est celle, asymptotique, des moindres carrés ; le niveau de 95 % et la réutilisation des tolérances d'accord sont des choix de la méthode. Il n'y a pas de durée minimale d'ajustement : c'est la précision qui décide si les périodes portent assez d'information. Les autres conditions ne changent pas : au moins 40 événements de température, une heure de chauffage équivalente à pleine puissance et un écart moyen intérieur-extérieur de 3 °C sur les morceaux ajustés.

**Accord avec le bilan énergétique.** Chaque ajustement de l'identification prend l'apport gratuit en vigueur comme une entrée de chaleur connue, $a(u_e + g_{EB})$ ; l'apport n'est pas ajusté fenêtre par fenêtre : sur les périodes calmes, $T_{in} - T_{ext}$ varie trop peu pour séparer une entrée constante de la perte statique. Un candidat (modèle avec murs ou modèle simple) n'est publié que si son propre $b_{eq}/a$ concorde avec $s_{EB}$ à 5 % près ($|\ln(\frac{b_{eq}/a}{s_{EB}})| \le \ln 1.05$) ; sinon la fenêtre ne publie rien (`the model's heat loss disagrees with the measured heat balance`). Le modèle en vigueur est le dernier modèle qualifié au moment de la tâche. C'est le seul contrôle de $b_{eq}/a$ qui ne vient pas des ajustements : sans modèle qualifié en vigueur, rien n'est publié (`waiting for a qualified heat balance to check the model's heat loss`, affiché avec le statut `learning`). La valeur publiée, la médiane des derniers candidats, est contrôlée de nouveau contre le modèle en vigueur lors de sa publication ; en cas de désaccord, la publication en vigueur est conservée. Un modèle de pièce publié par une version antérieure reste en vigueur ; la puissance de maintien exige tout de même l'accord avec le bilan thermique en vigueur (4.2).

$s_{EB}$ est borné à 3, comme $k_{ff}$. Une source de modèle de pièce s'applique à
plein poids (`warmup_scale = 1`) et n'utilise pas la perturbation non mesurée $d$ du
suivi. La partie statique suit la consigne ; la partie murs suit l'air suivi. Avec l'air
à la consigne, $u_{ff1} = (b_{air}(SP - T_{ext}) - w)/a$, avec $b_{air}$ le taux de perte
totale de l'air et $w$ l'état des murs.

**Transfert sans à-coup de la part statique.** Une variation discrète de la part statique passe dans l'intégrale,
de sorte que $u_{ff} + K_i \cdot I$ ne saute pas :

$$ I \leftarrow clamp\left(I - \frac{\Delta u_{ff}}{K_i}, -\frac{2}{K_i}, \frac{2}{K_i}\right) $$

Les variations discrètes de la part statique sont un changement entre la source apprise et une source de modèle de pièce
(y compris le passage du poids du warm-up appris au plein poids), un changement de structure du modèle (modèle simple et
modèle avec murs), une nouvelle publication quotidienne des paramètres et un nouveau modèle
statique ($s_{EB}$ ou $g_{EB}$) en vigueur. $\Delta u_{ff}$ est la nouvelle part statique
moins l'ancienne (le FF1 appris pour la source apprise), tous deux à la consigne et à la température extérieure
courantes, et tous deux avec la part des murs appliquée à ce calcul, dans la borne $[0, 1]$. Le passage entre
`room model (air and walls)` et `room model (static part)` ne change pas la part statique : rien n'est transféré.
L'évolution continue de la consigne et de la température extérieure n'est pas transférée : c'est l'action du
feed-forward. L'évolution propre de la source apprise (rampe de warm-up, nouveaux `a`/`b`) n'est jamais transférée non
plus. Le transfert est appliqué dans le calcul, avant le PI, et peut traverser zéro ; il n'est limité que par les bornes
de l'intégrale. La saturation et l'anti-windup ne changent pas : ils agissent sur la commande totale.

**Fondu de la part des murs.** La part des murs n'est jamais déplacée dans l'intégrale. Lorsque le terme des murs en
vigueur change de façon discrète (activé lorsque le suivi devient `ok`, désactivé lorsqu'il n'est plus `ok` ou lorsque la
source apprise reprend la main, une publication qui change les paramètres des murs ; un nouveau modèle statique seul ne démarre pas de fondu), la part des murs appliquée passe
linéairement sur `RM_WALL_FADE_S` = 30 min de la valeur appliquée en dernier à sa cible. La cible est le
$\delta_{wall}/a$ courant d'un suivi `ok`, ou 0 lorsque le terme des murs est désactivé ; un fondu de sortie ne relit
jamais le suivi. Une nouvelle discontinuité pendant un fondu en démarre un nouveau à partir de la valeur appliquée à cet
instant, de sorte qu'un suivi qui alterne entre `ok` et non `ok` ne déplace pas l'intégrale et ne fait pas sauter la commande.
Lorsque la source apprise reprend la main en chauffage, un fondu de sortie se poursuit sur la puissance de maintien
apprise ; hors chauffage, la part des murs est abandonnée. Hors fondu, la part des murs appliquée suit le
$\delta_{wall}/a$ courant. La puissance de maintien reste bornée à $[0, 1]$, et `feedforward.wall_power_percent` affiche la
part des murs appliquée.

**Fenêtre ouverte.** Il n'y a aucun maintien après une fenêtre. Pendant qu'elle est ouverte, le suivi cale l'air sur la mesure et y entraîne les murs ; la source est `room model (static part)` et la part des murs sort en fondu. À la reprise, l'air est recalé sur la mesure, ses corrélations avec les murs et la perturbation sont abandonnées et les statistiques d'innovation repartent ; le suivi est de nouveau `ok` aussitôt et la part des murs revient en fondu sur `RM_WALL_FADE_S`. Une fenêtre ouverte élève l'échange réel air-murs au-dessus de celui du modèle, si bien que les murs suivis tendent à être plus chauds que les murs réels et que la part des murs est sous-estimée. Une sonde proche de la fenêtre peut biaiser dans l'autre sens ; la borne sur le terme des murs et les contrôles d'innovation couvrent ce cas.

**Redémarrage.** La source en vigueur est sauvegardée avec l'intégrale (`ff_source`), et le fondu de la part des murs
avec elle (`ff_wall` : la part des murs appliquée, la valeur d'où part un fondu et son instant de départ sur l'horloge
réelle). Au premier calcul après un redémarrage, une source différente (fichier de stockage du modèle de pièce perdu,
suivi qui repart) est transférée comme tout changement de source et démarre un fondu des murs à partir de la valeur
appliquée restaurée ; la même source reprend sans transfert, et un fondu en cours se poursuit là où l'horloge réelle le
place. Un état sauvegardé sans `ff_source` est pris pour la source apprise ; un état sans `ff_wall` valide applique
entièrement le terme des murs de sa source sauvegardée. Une réinitialisation de la mémoire du PI (changement de mode
HEAT/COOL, pause prolongée, réinitialisation de l'apprentissage) oublie la source et la part des murs appliquée : la source
suivante démarre sans transfert et sa part des murs se fond à partir de 0.

### 4.3 Gestion de consigne

La régulation travaille sur la consigne brute en HEAT comme en COOL. Les branches I et P lisent la même consigne ; les erreurs P et I lisent une température anticipée (voir **Anticipation** ci-dessous), qui est le seul mécanisme abaissant la commande en avance de la consigne.

`SmartPISetpointManager` conserve l'état du boost. Un changement de consigne significatif (`SETPOINT_BOOST_THRESHOLD = 0.3 °C`) active le boost, qui relève la limite de pente de la sortie (`SETPOINT_BOOST_RATE`) jusqu'à ce que l'erreur passe sous `SETPOINT_BOOST_ERROR_MIN = 0.3 °C`. La détection d'un changement de consigne qui invalide les fenêtres d'apprentissage est faite par l'orchestrateur, à partir de la variation de la consigne brute (supérieure à `0,01 °C`).

#### Anticipation (chauffage et refroidissement)

En HEAT et en COOL, en phase stable, l'erreur proportionnelle et l'erreur intégrale lisent une température anticipée : la température mesurée plus une correction $c$, la distance que la pièce est sur le point de parcourir pendant le temps entre une commande et son effet. La deadband, la near-band, les gardes, le feed-forward et l'apprentissage conservent la température mesurée.

La correction est la pente $s$ des moindres carrés de l'historique de température brute sur les $W = \min(\max(\text{ANTICIPATION\_MIN\_WINDOW\_S},\ H),\ \text{ANTICIPATION\_MAX\_WINDOW\_S})$ dernières secondes ($H$ est l'horizon ci-dessous), multipliée par $H$ et limitée à $\pm$ `ANTICIPATION_MAX_C` (`1.0 °C` ; `ANTICIPATION_MIN_WINDOW_S = 600 s`, `ANTICIPATION_MAX_WINDOW_S = 1800 s`). Une fenêtre plus courte que $H$ transformerait un saut rapide de la sonde en une grande correction. Elle vaut 0 sans horizon, hors phase stable, avec moins de 3 échantillons, ou lorsque les échantillons couvrent moins de la moitié de la fenêtre. Elle s'établit ensuite progressivement avec la distance à la consigne : avec $e = $ cible $-$ température mesurée,

$$c \leftarrow c \cdot \mathrm{clamp}\left(2 - \frac{e}{c},\ 0,\ 1\right)$$

donc la correction vaut 0 lorsque la pièce se rapproche de la consigne et en est plus éloignée que $2c$, elle est pleine à moins de $c$ de celle-ci, au-delà de la consigne, ou lorsque la pièce s'éloigne de la consigne.

Le rapport $e / c$ ne dépend pas du mode : en COOL, la pièce qui approche la consigne depuis le haut a $e < 0$ et $c < 0$, si bien que la règle est la même dans les deux modes.

Dans la deadband du régulateur (état laissé par le tick précédent), la correction est conservée jusqu'à ce qu'elle vaille 0 ou que son signe diffère de celui du tick précédent, puis elle vaut 0 tant que la pièce reste dans la deadband. Une pièce qui arrive à sa consigne garde son freinage, donc la commande ne saute pas à l'entrée ; une pièce installée à sa consigne ne s'en approche pas, et une petite variation rapide de la sonde serait sinon extrapolée sur tout l'horizon. La correction reprend lorsque la pièce quitte la deadband.

Les erreurs sont calculées sur la température anticipée $T + c$ avec le signe du mode (cible $-$ température en HEAT, température $-$ cible en COOL) :

- HEAT : $e_P = \text{cible} - (T + c)$ et $e_I = (\text{cible} - T) - c$,
- COOL : $e_P = (T + c) - \text{cible}$ et $e_I = (T - \text{cible}) + c$.

L'horizon $H$ est le temps entre une commande et son effet sur la pièce. Il se lit selon le mode, avec la même source de temps mort que la règle des gains (`gain_dead_time`) :

- en HEAT, la médiane des derniers retards de l'émetteur mesurés par le bilan de chauffe (les retards retenus, section 3.7) lorsqu'elle est positive, source publiée `heating balance` ;
- sinon, et toujours en COOL (le bilan de chauffe ne mesure qu'en chauffage), le plus long entre le temps mort actif du mode (le temps mort de chauffe en HEAT, le temps mort de refroidissement en COOL) et la moitié de son temps mort passif (le temps mort de refroidissement en HEAT, le temps mort de chauffe en COOL), chacun compté seulement lorsqu'il est fiable et positif, source `dead times` ;
- sinon 0 : pas d'anticipation, source `none`.

`analysis.control` publie `anticipation_c` (la correction appliquée au dernier tick), `anticipation_horizon_s` et `anticipation_horizon_source`. `temperature.anticipated` publie la température anticipée (température mesurée plus correction), dans `live` et dans `history`.

Le bloc canonique `live.setpoint` publie `boost_active`.

Aucun plafond de commande n'est appliqué après le calcul PI : la commande PI passe directement par les contraintes douces (limiteur de pente, `max_on_percent`) puis par les contraintes de temporisation.

Après un changement de consigne ou une reprise, l'intégrale suit l'erreur brute
à la consigne sans règle de rattrapage supplémentaire : seuls l'anti-windup
(ci-dessous), les modes de deadband et de near-band, le clamp de dépassement et
la coupure de surchauffe la limitent.

Les reprises suivant détection d'ouverture et power shedding gèlent l'intégrale
pendant le temps mort en chauffage :

- à la reprise, SmartPI arme un `I:HOLD` explicite (`window_resume` ou
  `power_shedding_resume`),
- ce hold reste actif tant que la réaction de chauffe est encore dans la fenêtre exploitable de `deadtime_heat`,
- à la fin de cette fenêtre, l'intégrale reprend son fonctionnement normal,
- une reprise dont l'erreur résiduelle signée est déjà sous le plancher
  d'erreur résiduelle termine le hold avant le début de la fenêtre de temps mort,
- sans temps mort de chauffe fiable, aucun hold n'est appliqué.

Cette séquence évite de faire apprendre à l'intégrale une dynamique de reprise encore dominée par le temps mort de chauffe.

Les états transitoires de rattrapage ne sont pas restaurés après reboot :

- tout `integral_hold_mode` temporaire est effacé.

Le redémarrage repart donc d'un état PI persistant utile, mais sans restaurer des états servo transitoires qui ne sont plus physiquement valides hors de leur session d'origine. Le timestamp mural de la dernière régulation active valide est persisté et n'est pas rafraîchi lorsque le mode HVAC est OFF. Si l'inactivité dépasse 96 heures, la mémoire PI du contrôleur est purgée à la reprise tandis que le modèle A/B appris reste disponible. Si le mode HVAC après un redémarrage diffère du mode du modèle persisté (HEAT versus COOL), l'intégrale est effacée. Lorsqu'une itération de régulation est ignorée parce que le capteur de température est absent ou que `guard_cut` est actif, le suivi du temps repart comme après un passage en OFF, afin que l'erreur ne soit pas intégrée sur la période ignorée.


### 4.4 Deadband, near-band et protections

`DeadbandManager` gère :

- une deadband symétrique avec hystérésis absolue (`DEADBAND_HYSTERESIS = 0.025`),
- une near-band asymétrique en chauffage ; en refroidissement, la near-band utilise la valeur configurée `near_band_deg` des deux côtés de la consigne,
- un calcul automatique de near-band en chauffage uniquement, si `deadtime_heat` et le modèle sont exploitables,
- un repli sur les seuils configurés sinon.

Les protections complémentaires actuellement présentes sont :

- `SmartPIGuards` (chauffage uniquement, hors bootstrap et impulsions d'une calibration) : `guard_cut` force la sortie à 0 lorsque la température mesurée dépasse `setpoint + near_band_above` pendant que le chauffage est actif, jusqu'à ce qu'elle redescende à ce seuil diminué du plus grand de `NEAR_BAND_HYSTERESIS_C = 0.05°C` et de `GUARD_CUT_RESET_STEPS = 1.5` pas de capteur observés (sans quoi une mesure qui oscille entre deux paliers autour du seuil relancerait la coupure, et le cycle, à chaque mesure). Pendant une coupure, la sortie reste à 0 (`I:OVERRIDE(guard_cut)`). Tant que le PI demande encore de la puissance, une intégrale positive suit les 0 % imposés par back-calculation, `d(Ki·I)/dt = −v/Ti` avec `v` la demande du PI et `Ti = Kp/Ki`, au plus un cycle par itération. Sa vitesse suit la demande, pas le dépassement : une intégrale qui porte plus que le besoin de chauffe redescend en quelques coupures, même quand la pièce dépasse à peine le seuil. Elle redescend jusqu'au point où elle seule maintiendrait la pièce au seuil de coupure diminué de `NEAR_BAND_HYSTERESIS_C` (`Ki·I/Kp`) ; en dessous, elle est conservée, de même que le besoin de chauffe qui reste après une baisse de consigne. Sans cela, un biais de l'intégrale restituait le même excès de puissance à chaque fin de coupure, et la pièce cyclait entre coupure et reprise. Le filtre de température, le détecteur de temps mort, la température publiée et l'apprentissage continuent de suivre la pièce. Le PI n'est pas calculé pendant une coupure : l'écart publié (`temperature.error`, `analysis.control.error_p`) est la consigne moins la température du calcul, l'anticipation publiée vaut `0` et `control.mode` vaut `heating cut above the setpoint` ; les termes d'écart internes et l'anticipation gardent leurs valeurs pour la fin de la coupure. La pièce refroidit alors sans chauffage, par exemple pendant toute la descente qui suit une baisse de consigne : c'est une fenêtre passive pour `b` et pour la ligne de base du retard de l'émetteur. La gouvernance y voit une commande nulle hors de toute bande, et les fenêtres `a` sont bloquées (`skip: guard cut`). L'apprentissage reste contigu à la fin de la coupure ; `guard_kick` relance le cycle lorsque, dans la near-band, la température descend sous `setpoint - near_band_below` alors que la commande est inférieure à 99 %. En refroidissement, aucune des deux gardes n'agit et la near-band n'est pas calculée automatiquement. Pendant le bootstrap, le chauffage fonctionne entre ses propres seuils d'hystérésis et une garde en cours est relâchée,
- anti-windup à chaque tick sur la limite haute applicable (voir ci-dessous),
- `I:HOLD` explicite pendant la phase post-reprise liée à `deadtime_heat` pour `window_resume` et `power_shedding_resume` en chauffage,
- `I:HOLD` avec source `vt_safety` pendant que le mode sécurité de Versatile Thermostat applique sa puissance par défaut à la place de la commande SmartPI,
- `I:HOLD` avec source `deadband_hysteresis_shell` tant que l'état de deadband hystérétique est actif et que l'erreur est hors de la deadband centrale,
- `I:HOLD` avec source `governance_sensor_invalid` tant qu'aucune température extérieure n'a été reçue (régime `DEGRADED`),
- `I:HOLD` avec source `calibration_recovery` après chaque impulsion de calibration jusqu'à ce que la pièce atteigne la consigne,
- gel de P et I dans la deadband centrale.

L'anti-windup agit à chaque calcul, dans l'espace de demande linéaire, sur la limite haute applicable : `1`, ou l'image linéaire de `max_on_percent` lorsqu'elle est plus basse. Il comporte deux parties :

- intégration conditionnelle : l'intégrale cesse de croître lorsque la demande non contrainte dépasse la limite haute applicable. À moins de `NEAR_SETPOINT_UNWIND_STEPS = 1.5` pas de capteur observés de la consigne, une sortie saturée par rapport à l'erreur laisse tout de même l'intégrale revenir vers zéro, sans le franchir (`I:UNWIND(SAT_HI|SAT_LO)`), à condition que la sortie sans le terme proportionnel (`u_ff + Ki·I`) reste dans ses limites : avec un capteur quantifié, une mesure à un pas de la consigne sature la sortie par le seul terme proportionnel, et une intégrale bloquée combinée au gel de deadband maintiendrait un écart permanent. Une sortie tenue à sa limite par `u_ff + Ki·I` (par exemple à `max_on_percent`) conserve l'intégrale, qui porte alors le biais du modèle ;
- rétro-calculation : à chaque tick dont le mode intégral est `I:RUN`, `I:SKIP` ou `I:CLAMP`, l'excès de la sortie PI (`u_ff + u_pi`, bornée à `[0, 1]`) au-delà de l'image linéaire de `max_on_percent` corrige l'intégrale de `beta * excès / Ki`, avec `beta = dt / max(Kp/Ki, dt)` (constante de temps de poursuite égale à `Ti = Kp/Ki`). La correction ne déplace l'intégrale que vers zéro et s'arrête à zéro : un excès porté par le seul terme proportionnel n'entraîne jamais l'intégrale dans l'autre sens. L'intégrale reste bornée à `±2/Ki`. Seule l'intégrale change : `u_pi` conserve la décomposition qui a produit la commande.

Ne sont pas traités comme une saturation : la quantification de Versatile Thermostat et la rétention de vanne (`dpercent`, période), les durées minimales on/off et le limiteur de pente. Le comportement est symétrique en HEAT et COOL, n'utilise pas les accusés de commande de Versatile Thermostat et est identique avec les anciennes versions de Versatile Thermostat. Le diagnostic `aw_du` est la correction (excès, `<= 0`) appliquée au dernier tick.

Le calcul distingue aussi deux zones autour de la consigne :

- la deadband hystérétique, utilisée pour stabiliser l'état `in_deadband`,
- la deadband centrale réelle, définie par `abs(error_i) < deadband_c`, utilisée pour geler effectivement P et I.

Cette séparation évite de garder P et I gelés uniquement parce que l'hystérésis maintient encore l'état de deadband alors que l'erreur a déjà quitté la deadband configurée.

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
| `deadtime_estimator.py` | `DeadTimeEstimator`, débuts d’épisode actif et fenêtre de temps mort |
| `ab_aggregator.py`     | agrégation médiane ou médiane pondérée des mesures `a/b` |
| `ab_drift.py`          | détection et recentrage de dérive persistante sur `a/b`, avec une séquence de candidats par canal |
| `learning_window.py`   | gestion des fenêtres d'apprentissage et de leurs gardes  |
| `ab_window_balance.py` | bilan intégral des fenêtres sans pente robuste et pools `a/b` |
| `response_lag.py`      | `ResponseLagEstimator` : retard de l'émetteur sur les phases de chauffe à puissance constante, portes de stabilisation et modèle de puissance délivrée |
| `heating_balance.py`   | `HeatingBalance` : vitesse de chauffe et retard de l'émetteur mesurés sur les phases de chauffe |
| `peak_search.py`       | recherche du pic de température après une coupure |
| `sensor_step.py`       | pas de la sonde, lu sur la dernière heure d'échantillons |
| `measure_event.py`     | détecteur des événements de mesure et enregistrement de la puissance et des températures |
| `event_reading.py`     | lecture des événements du bootstrap et des impulsions, valeurs retenues par mode ; lecteur de chaque événement pour le modèle de réponse |
| `measure_pulse.py`     | impulsions de mesure du démarrage direct                 |
| `event_fit.py`         | ajustement de la réponse de la pièce à un événement de chauffe, et ajustement par profils du modèle de réponse (travaux d'arrière-plan) |
| `response_model.py`    | temps mort et constante de temps de réponse par mode, publiés seulement |
| `temperature_history.py` | historique brut et filtré de la température de la pièce lu par l'apprentissage |
| `starting_values.py`   | valeurs de départ de `a` et `b`, relâchement et crédit de montée en charge |
| `learning_start.py`    | début de l'apprentissage (phase de l'algorithme, source de la commande, transmission des valeurs retenues) |
| `learning_outcomes.py` | compteurs bornés des issues de fenêtres d'apprentissage  |
| `gains.py`             | calcul et gel des gains                                  |
| `controller.py`        | PI discret, anti-windup, maintien, hystérésis, transferts d'intégrale |
| `deadband.py`          | deadband et near-band                                    |
| `setpoint.py`          | consigne de régulation et état du boost |
| `anticipation.py`      | horizon et correction de l'anticipation de la température mesurée (chauffage et refroidissement) |
| `resume_hold.py`       | maintien I:HOLD pendant le temps mort après reprise de fenêtre ou de délestage, plancher d'erreur résiduelle |
| `feedforward.py`       | calcul de `u_ff1`, gain FF signé `b/a`, facteur de montée en charge et FF1 du modèle de pièce |
| `room_model/feedforward.py` | source de puissance de maintien issue du modèle de pièce publié et du modèle statique du bilan énergétique en vigueur, transfert sans à-coup des variations discrètes de sa part statique, fondu de sa part des murs, gain statique pour la prédiction de freinage |
| `ff_ab_confidence.py`  | politique de confiance sur `a/b`                         |
| `governance.py`        | décisions de gel                                         |
| `calibration.py`       | calibration sur demande : demandes, limites des impulsions, fin, règle de correction, résumé publié |
| `guards.py`            | `guard_cut` et `guard_kick`                              |
| `restart_bridge.py`    | décision unique de pontage ou non d'un redémarrage, consultée par chaque composant, et contexte du dernier calcul |
| `quiet_periods/`       | obscurité d'après la position de la maison (`astral`), plateaux, séquences de baisse de consigne, épisodes, clés et début régulé des périodes calmes |
| `diagnostics.py`       | payload canonique des diagnostics live                  |
| `diagnostic_history.py` | enveloppe versionnée et séries Recorder stables         |
| `tint_filter.py`       | filtrage adaptatif de la température intérieure          |
| `timestamp_utils.py`   | conversion monotonic / wall-clock                        |

### 5.3 Composition interne de `SmartPI`

Les composants instanciés par `SmartPI.__init__()` sont notamment :

```python
self.gov = SmartPIGovernance(name)
self.sp_mgr = SmartPISetpointManager(name)
self.ctl = SmartPIController(name)
self.est = ABEstimator()
self.learn_win = LearningWindowManager(name)
self.deadband_mgr = DeadbandManager(name, near_band_deg)
self.gain_scheduler = GainScheduler(name)
self.tint_filter = AdaptiveTintFilter(name, enabled=ENABLE_ADAPTIVE_TINT_FILTER)
self.resume_hold = ResumeDeadtimeHold()
self.dt_est = DeadTimeEstimator()
self.guards = SmartPIGuards()
self._ab_confidence = ABConfidence()
self._ff_holding = HoldingTransfer()
```

### 5.4 Persistance actuelle

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
    "gs_state": {...},
    "guards_state": {...},
    "ff_source": {...},  # source de puissance de maintien que complète l'intégrale
    "ff_wall": {...},  # part des murs appliquée et son fondu
    "tint_filter_state": {...},
    "hysteresis_output": {...},  # bootstrap uniquement
    "measure_events": {...},  # enregistrement de la puissance et des températures (3.7)
    "event_reading": {...},  # lectures en cours et valeurs retenues (3.7)
    "calibration": {...},  # calibration en cours ou dernière (3.7)
    "restart": {...},  # dernier calcul, puissance engagée (pontage du redémarrage)
}
```

La boucle de régulation planifie au plus une écriture différée de ce payload par fenêtre de cinq minutes ; Home Assistant vide une écriture en attente à l'arrêt. Les services et la suppression de l'entité écrivent immédiatement un snapshot.

Les timestamps d'événements passés (historiques de température, début d'une calibration, période passive et épisode du retard de l'émetteur sur les phases de chauffe à puissance constante) sont persistés en temps mural et reconvertis en horloge monotonic au chargement, afin de conserver leur position dans le passé après un redémarrage ; en particulier, la limite de 48 heures d'une calibration continue de s'écouler après un redémarrage.

L'apprentissage du retard de l'émetteur conserve son propre stockage (version d'état 3) : historique, montées de confort en attente, catalogue des périodes calmes avec leurs clés, entrées de nuit, décisions sur les parties régulées, fenêtres, évaluations, valeurs confirmées par période et bornes inférieures, publications, le temps mort de bootstrap de référence figé au début de l'apprentissage et la position de la maison avec laquelle les preuves ont été trouvées. Un stockage de version 2 est lu en entier ; sa référence part du dernier temps mort de bootstrap fiable qu'il a enregistré, et il n'a pas de borne inférieure. Un stockage de version 1 ne conserve que son historique et ses hausses en attente : ses nuits, fenêtres, évaluations, confirmations et valeurs publiées avaient été sélectionnées par heures locales sans contrôle solaire et sont abandonnées, puis retrouvées à partir de l'historique. Un changement de position de la maison oublie les preuves des périodes. Une version plus récente est ignorée.

Le modèle de pièce conserve son propre stockage (version d'état 2) : paramètres publiés et leurs candidats, fenêtres et enregistrements indexés par leur dernière période calme, état de l'observateur avec la perturbation des périodes et les sous-pas en attente d'une période, bilan thermique en vigueur avec le dernier jour calculé, et heure de la dernière tâche d'identification. Un stockage de version 1 conserve son modèle publié, l'état physique de l'observateur (air, murs, perturbation, chronologie des commandes, scores des prévisions), le bilan thermique en vigueur et ses enregistrements ; ses fenêtres et candidats (indexés par jour, sélectionnés par heures locales) et ses perturbations nocturnes (sommées sur les heures locales, sans leurs sous-pas) sont abandonnés, et les fenêtres sont recalculées à partir des périodes calmes. Une version plus récente est ignorée.

#### Redémarrages

Versatile Thermostat n'envoie aucune commande au chauffage lorsque l'intégration est déchargée ou que Home Assistant s'arrête, ni au démarrage en chauffe ou en refroidissement avant son premier cycle : sur un arrêt court, le chauffage garde son dernier état commandé. Un tel arrêt n'est qu'un trou de quelques échantillons. `restart_bridge.py` décide une seule fois, au premier calcul d'un thermostat prêt (une consigne et une température intérieure sont disponibles) après un chargement, si le redémarrage est **ponté**. Jusque-là le redémarrage reste en attente : une sonde qui rapporte quelques secondes après le chargement ne l'empêche pas, et la durée d'arrêt est mesurée jusqu'au calcul qui décide. Toutes les conditions suivantes doivent être réunies :

- la durée d'arrêt, sur l'horloge murale, depuis le plus ancien dernier calcul des trois stockages (algorithme, apprentissage du retard de l'émetteur, modèle de pièce) jusqu'au premier calcul, est au plus `RESTART_BRIDGE_MAX_GAP_S = 600 s` ; un stockage écrit longtemps avant l'arrêt (une écriture différée perdue lors d'un plantage) rend la durée d'arrêt longue ;
- le mode HVAC est le même au dernier calcul et au premier, HEAT ou COOL, et la configuration du cycle (durée de cycle, délais minimaux de marche et d'arrêt, mode vanne) est la même ;
- l'état de l'actionneur a été sauvegardé : la puissance engagée dans le stockage de l'algorithme et, en HEAT, aussi l'état du cycle de chauffe dans l'historique du retard de l'émetteur et le dernier calcul du modèle de pièce, pour les stockages qui contiennent des données (un stockage d'ancien format n'en a aucun) ;
- SmartPI avait la main au dernier calcul et l'a au premier : ni fenêtre ouverte, ni délestage, ni mode sécurité, ni commande remplacée, ni état inconnu (après le redémarrage, l'état de la fenêtre, du délestage et de la sécurité du thermostat est lu avant toute commande) ;
- la première température intérieure passe la règle de saut de l'historique du retard de l'émetteur (un saut de plus de `0.5 °C` à plus de `20 °C/h` n'est pas physique) par rapport à la dernière, datée de son dernier changement ;
- l'horloge murale n'a pas sauté (pas de durée d'arrêt négative, pas de dernier calcul d'un stockage dans le futur).

Pendant que le redémarrage attend la température intérieure, un calcul avec une
consigne garde la commande engagée avant l'arrêt au lieu de forcer 0 : le chauffage
a conservé cette commande pendant l'arrêt, et la couper pour une sonde qui rapporte
quelques secondes plus tard interromprait la chauffe. La commande est gardée tant que
chaque règle ci-dessus qui ne lit pas la température intérieure est remplie au
calcul courant : l'arrêt et l'attente ensemble durent `RESTART_BRIDGE_MAX_GAP_S` au plus.
Sinon, la sortie vaut 0 comme pour tout capteur absent, et reste à 0 jusqu'à l'arrivée
de la température. Un redémarrage dont la sortie a été forcée à 0 pendant l'attente
n'est pas ponté (`indoor temperature unknown` lorsqu'aucune autre règle ne le refuse) :
le chauffage n'a pas conservé son état.

Chaque composant consulte cette décision ; aucun ne décide seul. L'état transitoire dont chaque composant a besoin est sauvegardé avec elle (en temps mural) et reste en attente jusqu'à la décision.

| Composant | Redémarrage ponté | Autre redémarrage |
|---|---|---|
| Puissance engagée | restaurée (le chauffage l'a conservée) ; le premier calcul couvre la durée d'arrêt avec elle | 0 jusqu'à la première commande appliquée |
| Suivi du temps | le premier calcul couvre la durée d'arrêt, sans le gel de l'apprentissage d'un cycle ; la mise en forme de la consigne repart encore de la consigne brute pendant ce cycle | premier calcul d'une nouvelle session, apprentissage gelé pendant un cycle |
| Détection du temps mort | la détection en cours reprend (3.6) | abandonnée, durée minimale OFF comptée depuis le chargement |
| Fenêtre d'apprentissage a/b | la fenêtre en cours reprend avec ses accumulateurs ; son segment de bilan intégral recommence | effacée |
| Retard de l'émetteur (phases à puissance constante) | période passive ou épisode repris (3.7) | épisode compté comme interrompu |
| Bilan de chauffe | période passive ou phase en cours reprise (3.7, bilan de chauffe) | phase abandonnée, mesures conservées |
| Lectures de mesure | l'enregistrement, les lectures en cours et la phase du détecteur reprennent (3.7) | les lectures en cours se terminent, l'enregistrement est abandonné, les valeurs retenues restent |
| Lectures du modèle de réponse | l'enregistrement et les lectures en cours reprennent (3.7) | les lectures en cours se terminent, l'enregistrement est abandonné, le modèle reste |
| Sortie tout-ou-rien du bootstrap | reprise dans la bande (3.1) | repart à 0 |
| Historique du retard de l'émetteur (HEAT uniquement ; en COOL comme un autre redémarrage) | la durée d'arrêt est un échantillon manquant (pas d'intervalle invalide) ; l'état du chauffage à l'arrêt est tenu jusqu'à la première commande qui suit, sans aucun front | la période de chauffe est close au dernier calcul, et seule la durée d'arrêt est invalide |
| Modèle de pièce (HEAT uniquement ; en COOL comme un autre redémarrage) | la commande appliquée à l'arrêt est tenue pendant la durée d'arrêt ; l'état est propagé à travers elle et les prévisions en cours se poursuivent | le chauffage est considéré comme éteint d'après l'état sauvegardé, l'incertitude est rouverte et les prévisions en cours sont abandonnées |

L'état du chauffage à l'arrêt est celui de l'écriture du stockage : un rechargement écrit les stockages au déchargement, et Home Assistant écrit les stockages en attente à son arrêt. Une période de chauffe prévue pour se terminer avant l'arrêt est terminée ; un chauffage allumé à l'arrêt reste allumé jusqu'à la première commande après le redémarrage.

Un redémarrage non ponté n'exclut de l'historique du retard de l'émetteur que sa durée d'arrêt (du dernier calcul au premier), et non une marge autour : l'état de l'actionneur pendant la durée d'arrêt est inconnu, mais les observations avant et après sont valides.

L'historique du retard de l'émetteur garde un repère de chaque redémarrage (dernier calcul, premier calcul, ponté) dans `restarts`, afin que les règles ultérieures voient encore un redémarrage qui n'a laissé aucun intervalle invalide. Le modèle de pièce a sa limite de reprise de 3 heures.

`control.last_restart` publie la décision de la session courante (5.5).

Les pools de bilan intégral et les compteurs d'issues de fenêtres ne sont pas
persistés : ils repartent vides à chaque démarrage. Les observations des mesures de
`a` sont persistées avec l'état de l'estimateur ; un état qui n'en contient pas se
charge avec des mesures non recalculables.

La puissance appliquée qui alimente le temps mort, l'apprentissage du retard de l'émetteur et le modèle de pièce vient des reçus de commande appliquée du scheduler de cycles de Versatile Thermostat (`AppliedCommandReceipt` de `vtherm_api`) : un reçu pour chaque commande qu'il applique réellement, un reçu `cycle_start` à chaque début de cycle maître et un reçu `update` pour chaque position de vanne appliquée en cours de cycle. Le reçu donne la puissance actionneur appliquée, convertie dans l'espace linéaire du modèle. SmartPI ne tague pas ses requêtes : il lit seulement les reçus.

Sans prise en charge des reçus par Versatile Thermostat, SmartPI engage la puissance indiquée par le callback de début de cycle, ou l'ouverture de vanne publiée en mode vanne (relue après une mise à jour en cours de cycle). Une mise à jour de vanne sans ouverture publiée n'engage rien.

### 5.5 Diagnostics publiés

`diagnostics.py` expose les vues algorithmiques et live canoniques :

- `SmartPI.get_published_diagnostics()` : renvoie le payload `live` canonique du capteur de diagnostic dédié,
- `build_published_diagnostics()` : la fonction qui construit ce payload.

`diagnostic_history.py` enveloppe ce payload dans `schema_version`, `live` et le
bloc stable de dix-neuf séries `history`. Les entités normales excluent `live` du
Recorder ; la sous-classe debug l'enregistre directement. Le mode debug ne fait que sélectionner la classe de capteur enregistrée ; il n'ajoute pas de second bloc de diagnostic.

`control.last_restart` donne la décision sur le redémarrage de la session courante : `bridged`, la durée d'arrêt `downtime_s` (en secondes, `null` lorsque l'horloge a sauté ou que rien n'a été sauvegardé) et, pour un redémarrage non ponté, la `reason` en mots simples. Il vaut `null` avant le premier calcul d'un thermostat restauré depuis un stockage, et pour un thermostat sans état sauvegardé.

`learning.start` est un résumé simple du démarrage de l'apprentissage (`mode`,
`pulses_done`, `pulse`, `values_kept`, `values_kept_at`, `kept_a`, `kept_b`,
`kept_lag_s`), construit par `LearningStart.public()` pour le mode dans lequel se
trouve le modèle. Les états d'impulsion y sont traduits (`none`, `rest`,
`full power`, `stop`).

`analysis.learning.learn_ok_count` et `learn_skip_count` sont des compteurs
d'exécution relatifs au processus Home Assistant courant. Les compteurs
persistés du modèle restent internes et servent aux décisions de qualité du
modèle et de warm-up. `analysis.learning.window_outcomes_24h` liste, pour `a` et
pour `b`, les huit motifs de fermeture de fenêtre les plus fréquents des
dernières 24 heures sous forme de paires `[motif, nombre]` (valeurs numériques
retirées des motifs ; les événements des pools sont préfixés par `pool:`).
`analysis.learning.balance_pool` donne le nombre de segments en attente de
chaque pool et le pas de capteur utilisé pour leur incertitude ; ces valeurs
sont propres à la session Home Assistant courante.

---

## 6. Constantes importantes

### 6.1 Apprentissage

| Constante                      | Valeur |
| ------------------------------ | ------ |
| `AB_HISTORY_SIZE`              | `31`   |
| `AB_MIN_SAMPLES_B`             | `5`    |
| `AB_MIN_SAMPLES_A`             | `5`    |
| `AB_MIN_SAMPLES_A_CONSISTENT`  | `4`    |
| `AB_A_CONSISTENT_MAX_REL_MAD`  | `0.10` |
| `AB_A_SOFT_GATE_MIN_B`         | `5`    |
| `AB_CONFIDENCE_MIN_SAMPLES_A`  | `11`   |
| `AB_CONFIDENCE_MIN_SAMPLES_B`  | `11`   |
| `AB_B_CONVERGENCE_MIN_SAMPLES` | `11`   |
| `AB_B_CONVERGENCE_MIN_BHIST`   | `5`    |
| `AB_B_CONVERGENCE_MAD_RATIO`   | `0.30` |
| `AB_B_CONVERGENCE_RANGE_RATIO` | `0.10` |
| `DT_DERIVATIVE_MIN_ABS`        | `0.05` |
| `OLS_MIN_JUMPS`                | `3`    |
| `OLS_MIN_SPAN_STEPS`           | `1.5`  |
| `OLS_SPAN_MIN_STEP_C`          | `0.05` |
| `OLS_T_MIN`                    | `2.5`  |
| `DT_MAX_MIN`                   | `240`  |
| `U_OFF_MAX`                    | `0.05` |
| `U_ON_MIN`                     | `0.20` |
| `AB_BALANCE_MAX_REL_UNC`       | `0.25` |
| `AB_BALANCE_MIN_SEGMENT_MIN`   | `15`   |
| `AB_BALANCE_MIN_RESOLUTION_C`  | `0.05` |
| `AB_BALANCE_DEFAULT_RESOLUTION_C` | `0.1` |
| `AB_BALANCE_POOL_MAX_SEGMENTS` | `12`   |
| `RESPONSE_LAG_HISTORY`         | `7`    |
| `RESPONSE_LAG_MIN_ESTIMATES`   | `3`    |
| `RESPONSE_LAG_RESOLVE_EPISODES` | `4`   |
| `RESPONSE_LAG_PHASE_MIN_OFF_S` | `600` |
| `RESPONSE_LAG_MAX_REL_UNC`     | `0.15` |
| `RESPONSE_LAG_UNC_FLOOR_S`     | `30`   |
| `RESPONSE_LAG_MIN_OFF_S`       | `1800` |
| `RESPONSE_LAG_MIN_OFF_LAGS`    | `5`    |
| `RESPONSE_LAG_MIN_CROSSINGS`   | `3`    |
| `RESPONSE_LAG_MODEL_CHANGE_REL` | `0.5` |
| `RESPONSE_LAG_U_TOL`           | `0.05` |
| `RESTART_BRIDGE_MAX_GAP_S` (`RESPONSE_LAG_RESTART_MAX_GAP_S`) | `600` |
| `RESPONSE_LAG_PASSIVE_SPAN_S`  | `3600` |
| `RESPONSE_LAG_MIN_INVERSION_SLOPE` | `0.05` |
| `RESPONSE_LAG_RESOLVABLE_FRAC` | `0.9`  |
| `RESPONSE_LAG_B_REL_UNC`       | `0.10` |
| `RESPONSE_LAG_MAX_S`           | `DT_MAX_MIN` × 60 (`14400`) |
| `AB_SETTLE_K_ACTIVE`           | `3`    |
| `AB_SETTLE_K_PASSIVE`          | `3`    |
| `AB_SETTLE_ACTIVE_CAP_FRAC`    | `0.5`  |

### 6.2 Régulation

| Constante                     | Valeur  |
| ----------------------------- | ------- |
| `KP_SAFE`                     | `0.55`  |
| `KI_SAFE`                     | `0.010` |
| `GAIN_TI_LAMBDA_FACTOR`       | `4.0`   |
| `NEAR_SETPOINT_UNWIND_STEPS`  | `1.5`   |
| `RECOVERY_ERROR_FLOOR_DEADBAND_RATIO` | `2.0` |
| `ANTICIPATION_MAX_C` | `1.0` |
| `ANTICIPATION_MIN_WINDOW_S` | `600` |
| `ANTICIPATION_MAX_WINDOW_S` | `1800` |
| `GUARD_CUT_RESET_STEPS`       | `1.5`   |
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

### 6.4 Calibration

| Constante                         | Valeur    |
| --------------------------------- | --------- |
| `CALIBRATION_MAX_S`               | `172800`  |
| `CALIBRATION_MIN_INTERVAL_S`      | `86400`   |
| `CALIBRATION_CORRECT_REL`         | `0.30`    |
| `CALIBRATION_CORRECT_B`           | `True`    |
| `CALIBRATION_SKIP_WHEN_OWN_AGREE` | `False`   |
| `CALIBRATION_OWN_RECENT`          | `3`       |
| `CALIBRATION_START_OVER_C`        | `0.2`     |
| `CALIBRATION_GAP_MIN_S`           | `3600`    |
| `CALIBRATION_GAP_MAX_S`           | `21600`   |
| `CALIBRATION_REST_EXTRA_S`        | `1200`    |
| `CALIBRATION_HEAT_MIN_S`          | `7200`    |
| `CALIBRATION_STEP_MAX_S`          | `14400`   |
| `CALIBRATION_DEPTH_CAP_C`         | `0.8`     |
| `CALIBRATION_FALL_WINDOW_S`       | `900`     |

`CALIBRATION_MAX_S` est dans `const.py`, les autres dans `calibration.py`. Pour le reste, les impulsions d'une calibration utilisent les constantes des impulsions de mesure (`measure_pulse.py`).

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

La matrice pilote uniquement la mise à jour des gains et l'apprentissage thermique. Une transition de régime (`REGIME_TRANSITION`) gèle les gains et l'apprentissage mais ne maintient pas l'intégrale. L'intégrale n'est maintenue que par les sources listées dans les protections de la section 4 (deadtime, phases de reprise, `vt_safety`, `calibration_recovery`) et par deux sources liées à la gouvernance : `deadband_hysteresis_shell` (état de deadband hystérétique actif, erreur hors de la deadband centrale) et `governance_sensor_invalid` (régime `DEGRADED` avant la réception d'une première température extérieure). Lorsque la sonde extérieure cesse de fournir des valeurs après une première valeur, le régime est `DEGRADED` : les gains et l'apprentissage thermique restent gelés, l'intégrale continue d'intégrer, et la chaîne de commande utilise la dernière température extérieure valide.

---

## 8. Références

1. **Astrom K.J. et Hagglund T.**, travaux sur la régulation PID et l'usage de modèles FOPDT.
2. **Sundaresan K.R. et Krishnaswamy P.R.**, estimation des paramètres de délai et de constante de temps.
3. Régression **OLS** avec test t de Student pour la validation de pente.

Ce document sert de référence alignée sur l'implémentation SmartPI actuelle.
