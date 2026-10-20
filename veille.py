#!/usr/bin/env python3
"""Veille des offres de cours Complétude.

Se connecte à l'espace professeur, lance la même recherche que la page
« Mes offres de cours », garde les offres situées dans les départements
choisis et envoie une notification ntfy pour chaque nouvelle offre.

Usage :
    python3 veille.py            # passage normal (respecte les horaires)
    python3 veille.py --force    # ignore les horaires
    python3 veille.py --test     # envoie juste une notification de test
"""

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

DOSSIER = Path(__file__).resolve().parent
FICHIER_ENV = DOSSIER / ".env"
FICHIER_VUS = DOSSIER / "vu.json"
FICHIER_JETON = DOSSIER / ".jeton.json"

API = "https://sic.internetude.fr/Api/ProfesseurCompletude"
PAGE_OFFRES = "https://professeur.completude.com/cap"
RAYON_M = 50_000  # même zone que le site : 50 km autour de l'adresse


CLES_CONF = (
    "COMPLETUDE_IDENTIFIANT", "COMPLETUDE_MOT_DE_PASSE", "NTFY_CANAL", "NTFY_SERVEUR",
    "DEPARTEMENTS", "JOURS", "HEURE_DEBUT", "HEURE_FIN", "ADRESSE_ID",
    "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "GITHUB_DEPOT",
)
SUR_GITHUB = os.environ.get("GITHUB_ACTIONS") == "true"
DELAI_MAC_ABSENT = 8 * 60  # au-delà, GitHub considère le Mac éteint et prend le relais


def charger_env():
    """Réglages lus dans .env, puis dans les variables d'environnement (GitHub Actions)."""
    conf = {}
    if FICHIER_ENV.exists():
        for ligne in FICHIER_ENV.read_text(encoding="utf-8").splitlines():
            ligne = ligne.strip()
            if not ligne or ligne.startswith("#") or "=" not in ligne:
                continue
            cle, valeur = ligne.split("=", 1)
            conf[cle.strip()] = valeur.strip().strip('"').strip("'")
    for cle in CLES_CONF:
        if os.environ.get(cle):
            conf[cle] = os.environ[cle]
    if not conf.get("COMPLETUDE_IDENTIFIANT"):
        sys.exit("Identifiants manquants : remplis .env ou les secrets GitHub")
    return conf


def log(message):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def requete(url, donnees=None, entetes=None, methode=None, formulaire=False):
    entetes = dict(entetes or {})
    corps = None
    if donnees is not None:
        if formulaire:
            corps = urllib.parse.urlencode(donnees).encode()
            entetes["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            corps = json.dumps(donnees).encode()
            entetes["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=corps, headers=entetes, method=methode)
    with urllib.request.urlopen(req, timeout=30) as rep:
        return json.loads(rep.read().decode("utf-8"))


# --- Connexion -------------------------------------------------------------

def jeton_valide(jeton):
    try:
        expire = datetime.strptime(jeton[".expires"], "%a, %d %b %Y %H:%M:%S GMT")
        expire = expire.replace(tzinfo=timezone.utc) - timedelta(minutes=2)
        return datetime.now(timezone.utc) < expire and jeton.get("access_token")
    except (KeyError, ValueError, TypeError):
        return False


def obtenir_jeton(conf):
    jeton = None
    if FICHIER_JETON.exists():
        try:
            jeton = json.loads(FICHIER_JETON.read_text())
        except json.JSONDecodeError:
            jeton = None
    if jeton and jeton_valide(jeton):
        return jeton["access_token"]

    nouveau = None
    if jeton and jeton.get("refresh_token"):
        try:
            nouveau = requete(f"{API}/TokenAuth", {
                "refresh_token": jeton["refresh_token"],
                "grant_type": "refresh_token",
            }, formulaire=True)
        except urllib.error.HTTPError:
            nouveau = None
    if not nouveau:
        nouveau = requete(f"{API}/TokenAuth", {
            "username": conf["COMPLETUDE_IDENTIFIANT"],
            "password": conf["COMPLETUDE_MOT_DE_PASSE"],
            "grant_type": "password",
        }, formulaire=True)
        log("Connexion avec identifiant/mot de passe")
    FICHIER_JETON.write_text(json.dumps(nouveau))
    FICHIER_JETON.chmod(0o600)
    return nouveau["access_token"]


# --- Recherche -------------------------------------------------------------

def choisir_adresse(adresses, conf):
    voulu = conf.get("ADRESSE_ID")
    if voulu:
        for a in adresses:
            if str(a["Id"]) == voulu:
                return a
        sys.exit(f"ADRESSE_ID={voulu} introuvable parmi tes adresses")
    for a in adresses:
        if a.get("AdresseContrat"):
            return a
    return adresses[0]


def chercher_offres(jeton):
    entetes = {"Authorization": f"Bearer {jeton}"}
    adresses = requete(f"{API}/intervenant/adresses", entetes=entetes)
    adresse = choisir_adresse(adresses, CONF)
    session = requete(f"{API}/cap/session/init", entetes=entetes)
    session["IdAdresse"] = adresse["Id"]

    lat, lng = adresse["Latitude"], adresse["Longitude"]
    d_lat = 360 * RAYON_M / 40_075_017
    d_lng = d_lat / math.cos(math.radians(lat))
    corps = {
        "SessionRecherche": session,
        "MapBox": {
            "IdSessionRecherche": session.get("Id", 0),
            "NordOuestLatitude": lat + d_lat,
            "NordOuestLongitude": lng - d_lng,
            "SudEstLatitude": lat - d_lat,
            "SudEstLongitude": lng + d_lng,
        },
    }
    resultat = requete(f"{API}/cap/search", corps, entetes=entetes)
    return resultat.get("Demandes") or []


def departement(offre):
    """Département de l'offre via geo.api.gouv.fr (coordonnées GPS)."""
    if offre.get("Ville", "").upper().startswith("PARIS"):
        return "75"
    url = "https://geo.api.gouv.fr/communes?" + urllib.parse.urlencode({
        "lat": offre["Latitude"], "lon": offre["Longitude"],
        "fields": "codeDepartement",
    })
    communes = requete(url)
    return communes[0]["codeDepartement"] if communes else None


# --- Notifications ---------------------------------------------------------

def notifier(conf, titre, message, priorite=4, tags=None):
    """Envoie sur chaque canal configuré (ntfy et/ou WhatsApp)."""
    erreurs = []
    if conf.get("NTFY_CANAL"):
        try:
            requete(conf.get("NTFY_SERVEUR", "https://ntfy.sh").rstrip("/") + "/", {
                "topic": conf["NTFY_CANAL"],
                "title": titre,
                "message": message,
                "priority": priorite,
                "tags": tags or [],
                "click": PAGE_OFFRES,
            })
        except Exception as err:  # noqa: BLE001
            erreurs.append(f"ntfy : {err!r}")
    if conf.get("TELEGRAM_TOKEN") and conf.get("TELEGRAM_CHAT_ID"):
        try:
            requete(f"https://api.telegram.org/bot{conf['TELEGRAM_TOKEN']}/sendMessage", {
                "chat_id": conf["TELEGRAM_CHAT_ID"],
                "text": f"{titre}\n\n{message}\n\n{PAGE_OFFRES}",
                "disable_web_page_preview": True,
            })
        except Exception as err:  # noqa: BLE001
            erreurs.append(f"Telegram : {err!r}")
    for e in erreurs:
        log(f"Échec notification {e}")


def trouver_chat_telegram(conf):
    """Récupère l'identifiant de conversation après un /start envoyé au bot."""
    if not conf.get("TELEGRAM_TOKEN"):
        sys.exit("Remplis d'abord TELEGRAM_TOKEN dans .env")
    maj = requete(f"https://api.telegram.org/bot{conf['TELEGRAM_TOKEN']}/getUpdates")
    chats = {m["message"]["chat"]["id"] for m in maj.get("result", []) if "message" in m}
    if len(chats) != 1:
        sys.exit(f"{len(chats)} conversation(s) trouvée(s) : envoie /start à ton bot puis relance.")
    chat_id = str(chats.pop())
    lignes = FICHIER_ENV.read_text(encoding="utf-8").splitlines()
    lignes = [f"TELEGRAM_CHAT_ID={chat_id}" if l.startswith("TELEGRAM_CHAT_ID=") else l
              for l in lignes]
    FICHIER_ENV.write_text("\n".join(lignes) + "\n", encoding="utf-8")
    log("TELEGRAM_CHAT_ID enregistré dans .env")


def texte_offre(o):
    return (
        f"{o.get('LibelleMatiere')} · {o.get('LibelleCombinaison')}\n"
        f"{o.get('LibelleFrequence')} – {o.get('LibelleDuree')}\n"
        f"{o.get('DetailPrix')}\n"
        f"{o.get('DebutCours')}\n"
        f"Réf. {o.get('IdDemande')}"
    )


# --- Relais Mac / GitHub ---------------------------------------------------
# Le Mac est la veille principale. À chaque passage, il publie dans la variable
# GitHub ETAT_MAC l'heure de son passage et les offres déjà notifiées. GitHub ne
# prend le relais que si ce signal date de plus de DELAI_MAC_ABSENT ; il publie
# alors son vu.json en artefact, que le Mac récupère à son retour.

def commande_gh():
    return shutil.which("gh") or next(
        (p for p in ("/opt/homebrew/bin/gh", "/usr/local/bin/gh") if Path(p).exists()), None)


def gh(*args, **kwargs):
    return subprocess.run([commande_gh(), *args], check=True, timeout=60, **kwargs)


def relais_actif(conf):
    return not SUR_GITHUB and conf.get("GITHUB_DEPOT") and commande_gh()


def recuperer_notifs_github(conf, vus):
    """Côté Mac : ajoute aux offres vues celles traitées par GitHub en son absence."""
    depot = conf["GITHUB_DEPOT"]
    res = gh("api", f"repos/{depot}/actions/artifacts?name=vu-github&per_page=1",
             capture_output=True, text=True)
    artefacts = json.loads(res.stdout).get("artifacts") or []
    if not artefacts or artefacts[0]["expired"]:
        return
    ident = artefacts[0]["id"]
    if ident == vus.get("artefact_github"):
        return
    with tempfile.TemporaryDirectory() as dossier:
        archive = Path(dossier) / "vu.zip"
        with archive.open("wb") as f:
            gh("api", f"repos/{depot}/actions/artifacts/{ident}/zip", stdout=f)
        with zipfile.ZipFile(archive) as z:
            distantes = json.loads(z.read("vu.json")).get("offres", {})
    for cle, offre in distantes.items():
        vus["offres"].setdefault(cle, offre)
    vus["artefact_github"] = ident
    log(f"Relais GitHub : {len(distantes)} offres récupérées")


def signaler_mac_allume(conf, vus):
    """Côté Mac : signal de vie + offres déjà notifiées, lus par GitHub."""
    limite = time.time() - 14 * 86400
    notifiees = [k for k, v in vus["offres"].items() if v.get("notifie") and v["vu_le"] > limite]
    etat = json.dumps({"t": int(time.time()), "notifiees": notifiees})
    gh("variable", "set", "ETAT_MAC", "--repo", conf["GITHUB_DEPOT"], "--body", etat,
       capture_output=True)
    # GitHub coupe les tâches programmées d'un dépôt public inactif depuis 60 jours :
    # on les réactive une fois par jour.
    aujourd_hui = datetime.now().strftime("%Y-%m-%d")
    if vus.get("reactivation_github") != aujourd_hui:
        gh("api", "-X", "PUT",
           f"repos/{conf['GITHUB_DEPOT']}/actions/workflows/veille.yml/enable",
           capture_output=True)
        vus["reactivation_github"] = aujourd_hui
        FICHIER_VUS.write_text(json.dumps(vus, indent=1, ensure_ascii=False))


def mac_allume(vus):
    """Côté GitHub : True si le Mac a fait un passage récemment."""
    try:
        etat = json.loads(os.environ.get("ETAT_MAC") or "{}")
    except json.JSONDecodeError:
        return False
    for cle in etat.get("notifiees", []):
        vus["offres"].setdefault(cle, {"ville": None, "dep": None, "vu_le": time.time(),
                                       "notifie": True})
    return time.time() - etat.get("t", 0) < DELAI_MAC_ABSENT


# --- Programme principal ---------------------------------------------------

def dans_les_horaires(conf):
    maintenant = datetime.now()
    jours = {int(j) for j in conf.get("JOURS", "0,1,2,3,4").split(",")}
    debut = int(conf.get("HEURE_DEBUT", 9))
    fin = int(conf.get("HEURE_FIN", 18))
    return maintenant.weekday() in jours and debut <= maintenant.hour < fin


def charger_vus():
    if FICHIER_VUS.exists():
        try:
            return json.loads(FICHIER_VUS.read_text())
        except json.JSONDecodeError:
            pass
    return {"offres": {}, "derniere_alerte_erreur": 0}


def main():
    args = set(sys.argv[1:])
    if "--telegram-id" in args:
        trouver_chat_telegram(CONF)
        return
    if "--test" in args:
        notifier(CONF, "Test veille Complétude", "Les notifications fonctionnent ✅", 3)
        log("Notification de test envoyée")
        return
    if "--force" not in args and not dans_les_horaires(CONF):
        return

    vus = charger_vus()
    deps_voulus = {d.strip() for d in CONF.get("DEPARTEMENTS", "75,92,93").split(",")}

    if SUR_GITHUB:
        if mac_allume(vus) and "--force" not in args:
            log("Mac allumé : rien à faire")
            return
        (DOSSIER / "passage_github.txt").touch()  # dit au workflow de sauvegarder vu.json
    elif relais_actif(CONF):
        try:
            recuperer_notifs_github(CONF, vus)
        except Exception as err:  # noqa: BLE001 — le relais ne doit jamais bloquer le Mac
            log(f"Relais GitHub indisponible : {err!r}")

    try:
        offres = chercher_offres(obtenir_jeton(CONF))
    except Exception as err:  # noqa: BLE001 — on veut être prévenu de tout échec
        if isinstance(err, urllib.error.HTTPError) and err.code in (400, 401):
            FICHIER_JETON.unlink(missing_ok=True)
        log(f"Erreur : {err!r}")
        # Le serveur de Complétude a des ratés passagers : on ne prévient
        # qu'après 3 échecs d'affilée (environ 15 minutes de vraie panne).
        vus["echecs_consecutifs"] = vus.get("echecs_consecutifs", 0) + 1
        FICHIER_VUS.write_text(json.dumps(vus, indent=1, ensure_ascii=False))
        if (vus["echecs_consecutifs"] >= 3
                and time.time() - vus.get("derniere_alerte_erreur", 0) > 2 * 3600):
            notifier(CONF, "Veille Complétude en panne",
                     f"Impossible de récupérer les offres : {err}", 3, ["warning"])
            vus["derniere_alerte_erreur"] = time.time()
            FICHIER_VUS.write_text(json.dumps(vus, indent=1, ensure_ascii=False))
        sys.exit(1)

    nouvelles = 0
    for o in offres:
        ident = str(o["IdDemande"])
        if ident in vus["offres"]:
            continue
        try:
            dep = departement(o)
        except Exception as err:  # noqa: BLE001 — on réessaiera au prochain passage
            log(f"Département inconnu pour {ident} ({o.get('Ville')}) : {err!r}")
            continue
        vus["offres"][ident] = {"ville": o.get("Ville"), "dep": dep, "vu_le": time.time()}
        if dep in deps_voulus:
            notifier(CONF, f"📚 Nouvelle offre – {o.get('Ville')} ({dep})",
                     texte_offre(o), 5, ["rotating_light"])
            vus["offres"][ident]["notifie"] = True
            nouvelles += 1

    # On oublie les offres vues il y a plus de 60 jours.
    limite = time.time() - 60 * 86400
    vus["offres"] = {k: v for k, v in vus["offres"].items() if v["vu_le"] > limite}
    vus["derniere_alerte_erreur"] = 0
    vus["echecs_consecutifs"] = 0
    FICHIER_VUS.write_text(json.dumps(vus, indent=1, ensure_ascii=False))
    origine = " (relais GitHub)" if SUR_GITHUB else ""
    log(f"{len(offres)} offres, {nouvelles} nouvelle(s) notifiée(s){origine}")

    if relais_actif(CONF):
        try:
            signaler_mac_allume(CONF, vus)
        except Exception as err:  # noqa: BLE001
            log(f"Signal de vie GitHub non envoyé : {err!r}")


CONF = charger_env()

if __name__ == "__main__":
    main()
