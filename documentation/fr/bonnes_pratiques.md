# SmartPI : bonnes pratiques pour bien apprendre

SmartPI apprend votre pièce tout seul : sa vitesse de chauffe, ses pertes et le temps que met le radiateur à agir. Plus ses mesures sont propres, plus la régulation est juste. Ce guide résume ce qui l'aide, à chaque étape. Le détail se trouve dans la [documentation utilisateur](vtherm_smartpi.md).

## Avant tout

- **Une bonne sonde.** L'idéal est une sonde au 0,01 °C qui publie au moins toutes les 5 minutes. Une sonde au 0,1 °C qui publie à chaque changement convient aussi. Placez-la vers le milieu de la pièce, loin du radiateur, du soleil direct et des courants d'air.
- **La température extérieure** doit être disponible dans Versatile Thermostat.
- **La position de la maison** doit être renseignée dans Home Assistant (Paramètres > Système > Général). SmartPI s'en sert pour savoir s'il fait jour ou nuit et écarter les mesures faussées par le soleil.
- **La détection des fenêtres** de Versatile Thermostat doit être activée (capteur ou détection automatique).
- **Un cycle adapté à l'émetteur** : voir le tableau [Cycle PWM selon l'installation](vtherm_smartpi.md#cycle-pwm-selon-linstallation).
- **Ne changez pas les réglages** pendant que SmartPI apprend. Un réglage modifié à mi-parcours brouille les mesures.

## Choisir le démarrage

| | Amorçage (par défaut) | Démarrage direct |
| --- | --- | --- |
| Comment | Chauffe tout ou rien autour de la consigne jusqu'à ce que le modèle soit appris | Régulation normale dès la première minute, avec au plus trois impulsions de mesure légères |
| Durée | De quelques heures à deux jours | Premier modèle en 8 à 24 heures environ |
| Confort pendant l'apprentissage | La température oscille entre les deux seuils | La pièce reste proche de la consigne |
| Sonde | Toutes, avec des seuils adaptés | Sonde précise au 0,01 °C de préférence |
| Programme | Indifférent | Au moins un changement de consigne programmé chaque jour |

En cas de doute, gardez l'amorçage.

## Pendant l'amorçage

- **Laissez-le tourner sans interruption** pendant un ou deux jours : pas d'arrêt du thermostat, pas de réinitialisation.
- **Gardez une consigne stable**, nettement au-dessus de la température extérieure. Évitez les gros changements de programme tant qu'il n'est pas terminé.
- **Adaptez les seuils d'hystérésis à votre sonde** : voir [Hystérésis du bootstrap selon le capteur](vtherm_smartpi.md#hystérésis-du-bootstrap-selon-le-capteur). Avec une sonde grossière ou lente, des seuils trop serrés font durer l'amorçage, voire l'empêchent d'aboutir.
- **Les oscillations sont normales** : c'est ainsi que SmartPI mesure la pièce. Suivez la progression dans la carte de diagnostic.
- **Signalez les perturbations** (voir plus bas) : invités nombreux, cheminée, chaudière à l'arrêt.

## Pendant le démarrage direct

- **Programmez au moins un abaissement par jour**, par exemple 1 °C de moins la nuit. Une consigne fixe jour et nuit ne donne pas à SmartPI les mesures dont il a besoin.
- **Laissez les impulsions se faire.** Une impulsion dure en général deux à trois heures : un court repos, une chauffe à pleine puissance, puis un arrêt. Un changement de consigne, un arrêt du thermostat ou une pause de l'apprentissage pendant ce temps la fait perdre. Évitez de toucher à la consigne à la main pendant la première journée.
- Deux impulsions sont espacées d'au moins 6 heures. Il en faut deux qui concordent.

## En régulation, au quotidien

- **Gardez un programme régulier.** Chaque remontée de consigne après une période sans chauffe (le matin, par exemple) est une occasion de mesure qui affine le modèle.
- **Signalez ce que SmartPI ne peut pas voir :**
  - l'**entité de perturbation**, à « on » pendant un apport ou une perte de chaleur inhabituel (invités, cheminée, fort soleil, chauffage d'appoint) ;
  - l'**entité de chauffage indisponible**, à « on » quand le radiateur ne peut pas chauffer (chaudière arrêtée, priorité eau chaude, dégivrage, délestage) ;
  - le **service `set_smartpi_learning`** pour suspendre l'apprentissage à la main (travaux, absence inhabituelle).

  Voir [Suspendre l'apprentissage](vtherm_smartpi.md#suspendre-lapprentissage).
- **De petits dépassements sont normaux** : un ou deux dixièmes de degré après une montée, surtout avec un radiateur lent. Le modèle continue de s'affiner pendant les jours qui suivent.

## Quand demander une calibration

La calibration (service `force_smartpi_calibration`) refait une mesure de la pièce avec au plus trois impulsions près de la consigne, et corrige le modèle s'il est nettement faux. Elle est utile :

- **après une mise à jour depuis une version antérieure** de SmartPI, tant que le diagnostic affiche « previous version » comme source du modèle de réponse. SmartPI en lance une lui-même au bout de 3 jours, une nuit où la consigne reste fixe d'après les deux nuits précédentes (la position de la maison doit être renseignée) ; `calibration.automatic_status` dit ce qu'il attend. Vous pouvez aussi la lancer vous-même sans attendre ;
- après un changement de radiateur, de sonde ou de gros travaux ;
- si la pièce dépasse ou traîne de façon répétée.

Pour qu'elle réussisse :

- choisissez un moment où **la consigne ne changera pas pendant plusieurs heures** ; la nuit, à la consigne de nuit, convient bien ;
- **n'éteignez pas le thermostat** et ne suspendez pas l'apprentissage pendant ce temps ;
- le résultat apparaît dans la carte de diagnostic (`calibration.result`). Si une impulsion est interrompue ou si la calibration échoue, une notification Home Assistant en donne la raison ; vous pouvez relancer une calibration aussitôt.

## Quand réinitialiser l'apprentissage

Réinitialisez (service `reset_smartpi_learning`) seulement si la pièce a vraiment changé : nouveau radiateur, sonde déplacée, thermostat affecté à une autre pièce. Une mauvaise journée ne justifie pas de tout réapprendre : le modèle se corrige de lui-même, et une calibration suffit le plus souvent.
