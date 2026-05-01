# Versatile Thermostat SmartPI

[Lire la version anglaise](README.md)

<p align="center">
  <img src="assets/brand/logo.png" alt="SmartPI Logo" width="300" />
</p>

<p align="center">
  <strong>Régulation PI auto-adaptative pour Home Assistant</strong>
</p>

<p align="center">
  Un régulateur PI basé sur un modèle thermique qui apprend le comportement réel de votre pièce et adapte la régulation automatiquement — au-delà des approches à coefficients fixes.
</p>

## 🌟 Qu'est-ce que SmartPI ?

SmartPI est un algorithme de contrôle thermique avancé basé sur un modèle de premier ordre (1R1C) et non sur une boucle PID classique. Il apprend la capacité de chauffe de la pièce, le taux de perte thermique et les temps morts, puis utilise ce modèle pour calculer une commande de chauffage bien plus précise qu'un contrôleur proportionnel à temps fixe.

- **Modèle thermique 1R1C** : Apprend le gain de chauffe, le taux de déperdition et les temps morts de votre pièce à partir d'observations réelles — aucun réglage manuel nécessaire
- **Gains PI auto-calculés** : Calcule Kp et Ki automatiquement à partir de la constante de temps et du temps mort appris, via des règles IMC et heuristiques
- **Feed-forward basé sur le modèle** : Estime la puissance de maintien nécessaire au point de consigne, complétée par un biais lent et une correction prédictive FF3 activée par défaut en cas de perturbation
- **Trajectoire analytique de consigne** : Façonne la référence proportionnelle et applique un cap d'atterrissage basé sur le modèle en chauffage pour atteindre la cible rapidement tout en limitant le dépassement
- **Gouvernance Safety-First** : Une matrice par régime gèle ou déverrouille l'apprentissage et l'adaptation des gains selon le contexte opérationnel
- **Auto-calibration** : Surveille la qualité du modèle et déclenche une séquence de recalibration lorsque l'apprentissage stagne
- **Linéarisation de courbe de vanne** : Traduit la demande SmartPI en position de vanne adaptée au comportement non linéaire des vannes thermostatiques (TRV)
- **Diagnostics détaillés** : Publie l'état de l'apprentissage, l'état du modèle et les données de régulation — consultable via une carte Markdown Home Assistant dédiée

## 🔗 Intégration avec Versatile Thermostat

Cette intégration étend l'intégration populaire [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat) en ajoutant l'algorithme SmartPI comme plugin. Versatile Thermostat fournit déjà une gestion complète des thermostats dans Home Assistant, et avec SmartPI vous obtenez :

- des algorithmes de contrôle de température professionnels
- une intégration transparente avec les configurations VT existantes
- des options de personnalisation par appareil
- des valeurs par défaut globales pour une installation simplifiée

## 📦 Installation

### Via HACS (recommandé)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=KipK&repository=vtherm_smartpi&category=Integration)

1. Assurez-vous d'avoir [HACS](https://hacs.xyz/) installé dans Home Assistant
2. Cliquez sur le bouton ci-dessus ou ajoutez manuellement ce dépôt dans HACS
3. Recherchez "Versatile Thermostat SmartPI" dans HACS
4. Installez l'intégration
5. Redémarrez Home Assistant
6. Ajouter l'intégration `Vtherm_smartpi` depuis Paramètres / Appareils et Services / Integration
7. Configurez l'algorithme SmartPI dans vos appareils Versatile Thermostat

### Installation manuelle

1. Téléchargez la dernière version depuis la page [Releases](https://github.com/KipK/vtherm_smartpi/releases)
2. Extrayez le fichier `vtherm_smartpi.zip`
3. Copiez le dossier `custom_components/vtherm_smartpi` dans le répertoire `custom_components` de Home Assistant
4. Redémarrez Home Assistant
5. Configurez comme indiqué ci-dessus

## 📖 Documentation

La documentation complète est disponible en français :

- 🇫🇷 [Documentation utilisateur (Français)](documentation/fr/vtherm_smartpi.md)
- 🇫🇷 [Documentation technique (Français)](documentation/fr/technical_doc.md)

## 👥 Auteurs

- [@KipK](https://github.com/KipK)
- [@gael1980](https://github.com/gael1980)

## 🤝 Contribution

Les contributions sont bienvenues ! Consultez la documentation pour les lignes directrices de développement et les procédures de test.

## 🙏 Remerciements

- [@gael1980](https://github.com/gael1980) pour les fondations scientifiques et la théorie derrière SmartPI
- [@caiusseverus](https://github.com/caiusseverus) pour son [simulateur de chauffage](https://github.com/caiusseverus/heating-simulator) et son aide pour les tests de SmartPI et son expertise
- [@jmcollin](https://github.com/jmcollin78) pour le développement de [Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat)
- [@bontiv](https://github.com/bontiv) pour le site [Versatile Thermostat](https://www.versatile-thermostat.org/)
- Tous les testeurs qui ont aidé lors du développement de SmartPI

## ☕ Soutien

Si SmartPI vous est utile, vous pouvez soutenir son développement ici :

<p>
  <a href="https://www.buymeacoffee.com/kipk" target="_blank" rel="noopener noreferrer">
    <img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me a Coffee" width="217" height="60" />
  </a>
</p>

## 📄 Licence

Ce projet est distribué selon les conditions de licence Smart-PI. Voir [LICENSE.md](LICENSE.md) pour plus de détails.
