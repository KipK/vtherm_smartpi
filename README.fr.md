# Versatile Thermostat SmartPI

[Lire la version anglaise](README.md)

<p align="center">
  <img src="assets/brand/logo.png" alt="SmartPI Logo" width="300" />
</p>

<p align="center">
  <strong>Contrôle thermostatique proportionnel avancé pour Home Assistant</strong>
</p>

<p align="center">
  Améliorez le chauffage et la climatisation de votre domicile avec SmartPI, l'algorithme proportionnel de pointe conçu pour une gestion de température de haute précision.
</p>

## 🌟 Qu'est-ce que SmartPI ?

SmartPI est un algorithme de contrôle thermique avancé basé sur un modèle de premier ordre (1R1C) et non sur une boucle PID classique. Il apprend la capacité de chauffe de la pièce, le taux de perte thermique et les temps morts, puis utilise ce modèle pour calculer une commande de chauffage bien plus précise qu'un contrôleur proportionnel à temps fixe.

- **Contrôle de température précis** : maintient la température cible avec de faibles variations
- **Efficacité énergétique** : optimise les cycles de chauffe grâce au comportement thermique appris
- **Apprentissage adaptatif** : s'ajuste en continu selon le modèle thermique de la maison
- **Commande basée sur un modèle** : anticipe la réaction de la pièce aux changements de chauffage
- **Protections avancées** : inclut hystérésis, deadbands, anti-windup et gestion de reprise de consigne

SmartPI transforme le pilotage de votre thermostat en un algorithme conscient du modèle thermique réel de votre logement.

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
6. Configurez l'algorithme SmartPI dans vos appareils Versatile Thermostat

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

## 🚀 Fonctionnalités

- **Algorithme SmartPI** : contrôle proportionnel avancé pour une gestion de température précise
- **Intégration Versatile** : fonctionne parfaitement avec Versatile Thermostat
- **Configuration par appareil** : personnalisez pour chaque thermostat
- **Valeurs par défaut globales** : installation facile avec des paramètres de secours
- **Implémentation robuste** : inclut des protections comme anti-windup et hystérésis
- **Open Source** : développement transparent et communautaire

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

## 📄 Licence

Ce projet est distribué selon les conditions de licence Smart-PI. Voir [LICENSE.md](LICENSE.md) pour plus de détails.
