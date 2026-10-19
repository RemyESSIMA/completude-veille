# Veille des offres Complétude

Toutes les 5 minutes, du lundi au vendredi de 9h à 18h, `veille.py` se connecte à l'espace prof,
lance la recherche « Mes offres de cours » (50 km autour de l'adresse de contrat) et envoie une
notification ntfy pour chaque nouvelle offre située dans le 75, le 92 ou le 93.

- Réglages : `.env` (identifiants, canal ntfy, départements, horaires)
- Offres déjà vues : `vu.json` (supprimer ce fichier = tout renotifier)
- Journal : `veille.log`

## Commandes

```bash
python3 veille.py --test     # notification de test
python3 veille.py --force    # un passage maintenant, même hors horaires
```

## Planification (launchd)

```bash
cp fr.completude.veille.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/fr.completude.veille.plist
```

Arrêter : `launchctl unload ~/Library/LaunchAgents/fr.completude.veille.plist`

Le Mac doit être allumé et éveillé pendant les horaires surveillés.
