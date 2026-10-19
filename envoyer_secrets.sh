#!/bin/bash
# Copie les réglages sensibles de .env vers les secrets du dépôt GitHub, sans les afficher.
cd "$(dirname "$0")" || exit 1
depot=$(grep -E "^GITHUB_DEPOT=" .env | head -1 | cut -d= -f2- | tr -d "[:space:]\"'")
for cle in COMPLETUDE_IDENTIFIANT COMPLETUDE_MOT_DE_PASSE TELEGRAM_TOKEN TELEGRAM_CHAT_ID NTFY_CANAL; do
  # Même lecture que veille.py : espaces et guillemets autour de la valeur ignorés.
  valeur=$(grep -E "^${cle}=" .env | head -1 | cut -d= -f2- \
    | sed -E "s/^[[:space:]]+//; s/[[:space:]]+\$//; s/^[\"']//; s/[\"']\$//")
  if [ -n "$valeur" ]; then
    printf '%s' "$valeur" | gh secret set "$cle" ${depot:+--repo "$depot"} && echo "✓ $cle envoyé"
  fi
done
