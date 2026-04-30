"""Interface Streamlit pour l'outil de prospection."""

import streamlit as st
import pandas as pd
import json
import re
import base64
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import quote_plus

from scraper import scrape_google_maps
from email_enricher import enrich_emails
from entreprise_enricher import enrich_entreprises
from email_finder import enrich_nominative_emails, validate_scraped_emails
from scoring import calculate_scores, score_color, score_label
from database import (
    save_search, get_searches, get_search_results, delete_search,
    delete_all_history, update_entreprises,
    create_job, get_job, list_jobs, cancel_job, delete_job,
)
import perplexity_search

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:  # extension optionnelle ; sans elle, refresh manuel
    st_autorefresh = None


@st.cache_data(show_spinner=False)
def _load_communes():
    """Charge la liste des communes françaises depuis le fichier local.

    Format: [[label, lat, lng], ...]
    """
    path = Path(__file__).parent / "communes_france.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_COMMUNES_DATA = _load_communes()
# Liste des labels pour le selectbox
_COMMUNES_LABELS = [c[0] for c in _COMMUNES_DATA] if _COMMUNES_DATA else None
# Index pour retrouver lat/lng à partir du label
_COMMUNES_INDEX = {c[0]: (c[1], c[2]) for c in _COMMUNES_DATA} if _COMMUNES_DATA else {}

st.set_page_config(page_title="Prospection - Nexoflow Studio", page_icon="N", layout="wide")

# --- Couleurs du logo ---
# Bleu vif "STUDIO" : #0EA5E9
# Bleu lavande slashes : #B8C4E0

# --- CSS personnalisé ---
st.markdown("""
<style>
    /* Reset fond blanc */
    .stApp {
        background-color: #ffffff;
    }

    /* Motif géométrique subtil en fond */
    .stApp::before {
        content: '';
        position: fixed;
        top: 0;
        left: 0;
        width: 100%;
        height: 100%;
        pointer-events: none;
        z-index: 0;
        background-image:
            linear-gradient(30deg, #edf2f7 12%, transparent 12.5%, transparent 87%, #edf2f7 87.5%, #edf2f7),
            linear-gradient(150deg, #edf2f7 12%, transparent 12.5%, transparent 87%, #edf2f7 87.5%, #edf2f7),
            linear-gradient(30deg, #edf2f7 12%, transparent 12.5%, transparent 87%, #edf2f7 87.5%, #edf2f7),
            linear-gradient(150deg, #edf2f7 12%, transparent 12.5%, transparent 87%, #edf2f7 87.5%, #edf2f7),
            linear-gradient(60deg, #f1f5f9 25%, transparent 25.5%, transparent 75%, #f1f5f9 75%, #f1f5f9),
            linear-gradient(60deg, #f1f5f9 25%, transparent 25.5%, transparent 75%, #f1f5f9 75%, #f1f5f9);
        background-size: 80px 140px;
        background-position: 0 0, 0 0, 40px 70px, 40px 70px, 0 0, 40px 70px;
        opacity: 0.35;
    }

    /* Contenu au-dessus du motif */
    .stApp > * {
        position: relative;
        z-index: 1;
    }

    /* Logo fixe en haut à gauche */
    .logo-container {
        position: fixed;
        top: 12px;
        left: 20px;
        z-index: 1000;
    }
    .logo-container img {
        height: 32px;
        opacity: 0.9;
    }

    /* Titre principal */
    .main-title {
        text-align: center;
        font-size: 2rem;
        font-weight: 700;
        color: #1a1a1a;
        margin-top: 0.5rem;
        margin-bottom: 0.2rem;
        letter-spacing: -0.5px;
    }
    .main-subtitle {
        text-align: center;
        font-size: 1rem;
        color: #64748b;
        margin-bottom: 2rem;
        font-weight: 400;
    }

    /* Masquer le header Streamlit par défaut */
    header[data-testid="stHeader"] {
        background: transparent;
    }

    /* ====== TEXTE GLOBAL LISIBLE ====== */
    /* Labels des inputs */
    .stTextInput label, .stNumberInput label, .stSlider label,
    .stCheckbox label, .stSelectbox label {
        color: #1e293b !important;
        font-weight: 500 !important;
    }

    /* Texte des checkboxes */
    .stCheckbox span {
        color: #1e293b !important;
    }

    /* Expander header */
    .streamlit-expanderHeader, [data-testid="stExpander"] summary span {
        color: #1e293b !important;
        font-weight: 500 !important;
    }

    /* Texte helper / info */
    .stTextInput div[data-testid="stMarkdownContainer"],
    .stNumberInput div[data-testid="stMarkdownContainer"] {
        color: #475569 !important;
    }

    /* ====== INPUTS ====== */
    .stTextInput > div > div > input {
        border-radius: 8px;
        border: 1.5px solid #B8C4E0;
        padding: 0.6rem 1rem;
        font-size: 0.95rem;
        background: #ffffff;
        color: #1e293b;
    }
    .stTextInput > div > div > input:focus {
        border-color: #0EA5E9;
        box-shadow: 0 0 0 2px rgba(14, 165, 233, 0.2);
    }
    .stTextInput > div > div > input::placeholder {
        color: #94a3b8;
    }
    .stNumberInput > div > div > input {
        border-radius: 8px;
        border: 1.5px solid #B8C4E0;
        background: #ffffff;
        color: #1e293b;
    }
    .stNumberInput > div > div > input:focus {
        border-color: #0EA5E9;
        box-shadow: 0 0 0 2px rgba(14, 165, 233, 0.2);
    }

    /* ====== SLIDER ====== */
    .stSlider [data-testid="stThumbValue"] {
        color: #1e293b !important;
    }
    .stSlider [role="slider"] {
        background-color: #0EA5E9 !important;
    }
    div[data-baseweb="slider"] div[role="progressbar"] > div {
        background-color: #0EA5E9 !important;
    }

    /* ====== CHECKBOX custom ====== */
    .stCheckbox [data-testid="stCheckbox"] > label > div[role="checkbox"][aria-checked="true"] {
        background-color: #0EA5E9 !important;
        border-color: #0EA5E9 !important;
    }

    /* ====== BOUTON RECHERCHER ====== */
    .stButton > button[kind="primary"] {
        background: linear-gradient(135deg, #0EA5E9, #0284c7);
        color: #ffffff;
        border: none;
        border-radius: 8px;
        padding: 0.6rem 2rem;
        font-weight: 600;
        font-size: 1rem;
        letter-spacing: 0.3px;
        transition: all 0.2s;
    }
    .stButton > button[kind="primary"]:hover {
        background: linear-gradient(135deg, #0284c7, #0369a1);
        box-shadow: 0 4px 12px rgba(14, 165, 233, 0.3);
    }

    /* ====== EXPANDER ====== */
    [data-testid="stExpander"] {
        border: 1px solid #B8C4E0;
        border-radius: 8px;
        background: rgba(184, 196, 224, 0.06);
    }

    /* ====== DATAFRAME / TABLE ====== */
    .stDataFrame {
        border-radius: 10px;
        overflow: hidden;
    }
    table {
        width: 100%;
        table-layout: fixed;
        border-collapse: collapse;
        font-size: 0.85rem;
    }
    table th {
        background: #0EA5E9;
        color: #fff;
        padding: 8px 10px;
        text-align: left;
        font-weight: 600;
        white-space: nowrap;
    }
    table th.sortable {
        cursor: pointer;
        user-select: none;
        position: relative;
        padding-right: 18px;
    }
    table th.sortable:hover {
        background: #0284c7;
    }
    table th.sortable::after {
        content: '⇅';
        position: absolute;
        right: 4px;
        opacity: 0.5;
        font-size: 0.75rem;
    }
    table th.sortable.sort-asc::after {
        content: '▲';
        opacity: 1;
    }
    table th.sortable.sort-desc::after {
        content: '▼';
        opacity: 1;
    }
    table td {
        padding: 6px 10px;
        border-bottom: 1px solid #e2e8f0;
        overflow: hidden;
        text-overflow: ellipsis;
        word-break: break-word;
    }
    table tr:hover td {
        background: #f0f9ff;
    }
    /* Largeurs des colonnes */
    table th:nth-child(1), table td:nth-child(1) { width: 16%; }  /* Nom */
    table th:nth-child(2), table td:nth-child(2) { width: 18%; }  /* Adresse */
    table th:nth-child(3), table td:nth-child(3) { width: 11%; }  /* Téléphone */
    table th:nth-child(4), table td:nth-child(4) { width: 16%; }  /* Email */
    table th:nth-child(5), table td:nth-child(5) { width: 14%; }  /* Site Web */
    table th:nth-child(6), table td:nth-child(6) { width: 5%; text-align: center; }   /* Note */
    table th:nth-child(7), table td:nth-child(7) { width: 6%; text-align: center; }   /* Nb avis */
    table th:nth-child(8), table td:nth-child(8) { width: 14%; text-align: center; }  /* Score */
    table td a {
        display: inline-block;
        max-width: 100%;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        vertical-align: middle;
    }

    /* ====== BOUTONS DOWNLOAD ====== */
    .stDownloadButton > button {
        border-radius: 8px;
        border: 1.5px solid #B8C4E0;
        background: #ffffff;
        color: #1e293b;
        font-weight: 500;
        transition: all 0.2s;
    }
    .stDownloadButton > button:hover {
        border-color: #0EA5E9;
        color: #0EA5E9;
        background: #f0f9ff;
    }

    /* ====== PROGRESS BAR ====== */
    .stProgress > div > div > div {
        background-color: #0EA5E9 !important;
    }

    /* ====== MESSAGES ====== */
    .stSpinner > div {
        color: #0EA5E9 !important;
    }

    /* ====== SEPARATEUR ====== */
    .divider {
        height: 1px;
        background: linear-gradient(90deg, transparent, #B8C4E0, transparent);
        margin: 1.5rem 0;
    }

    /* ====== TABS ====== */
    .stTabs [data-baseweb="tab-list"] button {
        font-weight: 600;
        color: #64748b;
    }
    .stTabs [data-baseweb="tab-list"] button[aria-selected="true"] {
        color: #0EA5E9;
    }

    /* ====== HISTORIQUE : bouton supprimer ====== */
    .hist-delete button {
        background: none !important;
        border: 1px solid #e2e8f0 !important;
        color: #94a3b8 !important;
        font-size: 0.8rem !important;
        padding: 2px 12px !important;
        border-radius: 6px;
        transition: all 0.2s;
    }
    .hist-delete button:hover {
        color: #ef4444 !important;
        border-color: #ef4444 !important;
        background: #fef2f2 !important;
    }
</style>
""", unsafe_allow_html=True)

# --- Logo en haut à gauche ---
logo_path = Path(__file__).parent / "logo-nexoflow-studio.png"
if logo_path.exists():
    logo_b64 = base64.b64encode(logo_path.read_bytes()).decode()
    st.markdown(
        f'<div class="logo-container"><img src="data:image/png;base64,{logo_b64}" alt="Nexoflow Studio"></div>',
        unsafe_allow_html=True,
    )

# --- Header ---
st.markdown('<div class="main-title">Outil de Prospection</div>', unsafe_allow_html=True)
st.markdown('<div class="main-subtitle">Trouvez les coordonn\u00e9es d\'entreprises \u00e0 partir d\'un domaine d\'activit\u00e9 et d\'une zone g\u00e9ographique</div>', unsafe_allow_html=True)
st.markdown('<div class="divider"></div>', unsafe_allow_html=True)


# --- Helpers d'affichage ---
def _make_nom_link(nom, deja_connue=False):
    if not nom:
        return ""
    badge = ""
    if deja_connue:
        badge = (
            '<span style="background:#e2e8f0;color:#64748b;padding:1px 6px;'
            'border-radius:8px;font-size:0.75rem;margin-right:6px;">d\u00e9j\u00e0 vu</span>'
        )
    url = f"https://www.google.com/search?q={quote_plus(str(nom))}"
    return f'{badge}<a href="{url}" target="_blank" style="color:#0EA5E9;text-decoration:none;">{nom}</a>'


def _make_site_link(site):
    if not site:
        return ""
    href = site if site.startswith("http") else f"https://{site}"
    return f'<a href="{href}" target="_blank" style="color:#0EA5E9;text-decoration:none;">{site}</a>'


def _make_score_badge(score):
    if pd.isna(score) or score == "":
        return ""
    score = int(score)
    color = score_color(score)
    label = score_label(score)
    return (
        f'<span style="background:{color};color:#fff;padding:2px 8px;'
        f'border-radius:12px;font-size:0.85rem;font-weight:600;">'
        f'{score} ({label})</span>'
    )


# Hiérarchie de sources pour la prospection (plus bas = mieux).
# RH passe avant "site" (= contact@ scrapé) : un rh@/recrutement@ touche un
# interlocuteur ciblé, alors qu'un contact@ scrapé tombe souvent dans une
# boîte partagée triée par tout le monde.
_SOURCE_TIER = {
    "dirigeant": 1,
    "direction": 2,
    "metier": 2,
    "rh": 3,
    "site": 4,
    "contact": 5,
}

_CONFIDENCE_RANK = {"vérifié": 0, "valid": 0, "probable": 1, "catchall": 1, "incertain": 2}


def pick_best_email(entreprise):
    """Sélectionne le meilleur email pour la prospection.
    Retourne (email, confiance, source) ou ("", "", "").
    Confiance > Source (un scrape vérifié bat un dirigeant incertain).
    """
    candidates = []

    email_d = (entreprise.get("email_dirigeant") or "").strip()
    if email_d:
        conf_d = (entreprise.get("email_dirigeant_confiance")
                  or entreprise.get("email_dirigeant_confidence") or "")
        conf_d = {"high": "vérifié", "medium": "probable", "low": "incertain"}.get(conf_d, conf_d)
        candidates.append((email_d, conf_d or "incertain", "dirigeant"))

    email_s = (entreprise.get("emails") or "").strip()
    if email_s:
        conf_s = entreprise.get("emails_confiance") or ""
        if not conf_s:
            status = entreprise.get("email_status") or ""
            conf_s = {"valid": "vérifié", "catchall": "probable",
                      "public": "probable", "unknown": "incertain"}.get(status, "")
        candidates.append((email_s, conf_s or "incertain", "site"))

    for tup in (entreprise.get("emails_strategiques") or []):
        if isinstance(tup, (list, tuple)) and len(tup) >= 3:
            e, typ, conf = tup[0], tup[1], tup[2]
        elif isinstance(tup, (list, tuple)) and len(tup) == 2:
            e, typ, conf = tup[0], tup[1], ""
        else:
            continue
        if e:
            candidates.append((e, conf or "probable", typ or "contact"))

    if not candidates:
        return ("", "", "")

    def _rank(c):
        _email, conf, source = c
        return (_CONFIDENCE_RANK.get(conf, 3), _SOURCE_TIER.get(source, 9))
    candidates.sort(key=_rank)
    return candidates[0]


def _format_best_email(email, confidence, source):
    """Email + badge confiance + petit suffixe source pour le tableau."""
    if not email:
        return ""
    colors = {"vérifié": "#10b981", "probable": "#f59e0b", "incertain": "#94a3b8"}
    color = colors.get(confidence, "#94a3b8")
    label = confidence or "?"
    src_labels = {
        "dirigeant": "dirigeant", "direction": "direction", "metier": "métier",
        "site": "site", "rh": "RH", "contact": "contact",
    }
    mailto = '<a href="mailto:' + email + '" style="color:#0EA5E9;text-decoration:none;">' + email + "</a>"
    badge = ('<span style="background:' + color + ';color:#fff;padding:1px 6px;'
             'border-radius:8px;font-size:0.7rem;margin-left:6px;">' + label + "</span>")
    src_html = ""
    if source in src_labels:
        src_html = ('<span style="color:#94a3b8;font-size:0.7rem;margin-left:4px;">('
                    + src_labels[source] + ")</span>")
    return mailto + badge + src_html


def _format_email_dirigeant(email, confidence):
    """Formate un email dirigeant avec un badge de confiance coloré."""
    if not email:
        return ""
    colors = {"high": "#10b981", "medium": "#f59e0b", "low": "#94a3b8"}
    labels = {"high": "v\u00e9rifi\u00e9", "medium": "probable", "low": "incertain"}
    conf = confidence or "low"
    color = colors.get(conf, "#94a3b8")
    label = labels.get(conf, "incertain")
    mailto = f'<a href="mailto:{email}" style="color:#0EA5E9;text-decoration:none;">{email}</a>'
    badge = (
        f'<span style="background:{color};color:#fff;padding:1px 6px;'
        f'border-radius:8px;font-size:0.7rem;margin-left:6px;">{label}</span>'
    )
    return mailto + badge


def _format_email_scraped(email, status):
    """Formate un email scrapé avec un badge selon la validation SMTP."""
    if not email:
        return ""
    colors = {
        "valid": "#10b981", "catchall": "#f59e0b", "public": "#94a3b8",
        "unknown": "#94a3b8", "invalid": "#ef4444", "no_mx": "#ef4444",
    }
    labels = {
        "valid": "valide", "catchall": "catchall", "public": "perso",
        "unknown": "?", "invalid": "invalide", "no_mx": "no mx",
    }
    mailto = f'<a href="mailto:{email}" style="color:#0EA5E9;text-decoration:none;">{email}</a>'
    if not status:
        return mailto
    color = colors.get(status, "#94a3b8")
    label = labels.get(status, status)
    badge = (
        f'<span style="background:{color};color:#fff;padding:1px 6px;'
        f'border-radius:8px;font-size:0.7rem;margin-left:6px;">{label}</span>'
    )
    return mailto + badge


def _build_export_df(results):
    """Construit un DataFrame pour les exports (Excel/Airtable) avec UNE
    seule colonne Email = meilleur de prospection + Email Confidence + Email Source.

    Les anciennes colonnes (email_dirigeant, emails_strategiques, etc.) ne sont
    PAS exportées — elles polluent les outils de mailing aval.
    """
    df = pd.DataFrame(results)

    # Best email par ligne
    best = [pick_best_email(ent) for ent in results]
    df["Email"] = [b[0] for b in best]
    df["Email Confidence"] = [b[1] for b in best]
    df["Email Source"] = [b[2] for b in best]

    # Dirigeant (concaténation prénom + nom)
    if "dirigeant_prenom" in df.columns or "dirigeant_nom" in df.columns:
        p_col = df.get("dirigeant_prenom", "").fillna("") if "dirigeant_prenom" in df.columns else [""] * len(df)
        n_col = df.get("dirigeant_nom", "").fillna("") if "dirigeant_nom" in df.columns else [""] * len(df)
        df["Dirigeant"] = [("{} {}".format(p, n)).strip().title() for p, n in zip(p_col, n_col)]

    rename = {
        "nom": "Nom",
        "adresse": "Adresse",
        "telephone": "Téléphone",
        "site_web": "Site Web",
        "note": "Note",
        "nb_avis": "Nb avis",
        "score": "Score",
        "siren": "SIREN",
        "dirigeant_qualite": "Qualité Dirigeant",
        "contenu_site": "Contenu Site",
    }
    df = df.rename(columns=rename)

    cols = [
        "Nom", "Dirigeant", "Qualité Dirigeant", "SIREN",
        "Email", "Email Confidence", "Email Source",
        "Adresse", "Téléphone", "Site Web",
        "Note", "Nb avis", "Score",
    ]
    cols = [c for c in cols if c in df.columns]
    df = df[cols]
    return df


def _export_excel(df_export):
    """Génère un fichier Excel avec la feuille Prospects + 3 feuilles de suivi."""
    buffer = BytesIO()
    df_suivi = pd.DataFrame(columns=["Destinataire", "Date et Heure", "Objet", "Corp"])
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df_export.to_excel(writer, index=False, sheet_name="Prospects")
        df_suivi.to_excel(writer, index=False, sheet_name="Envoi")
        df_suivi.to_excel(writer, index=False, sheet_name="Relance 1")
        df_suivi.to_excel(writer, index=False, sheet_name="Relance 2")
    return buffer.getvalue()


def render_results_table(results, show_deja_connue=False):
    """Construit et affiche le tableau HTML des résultats."""
    df = pd.DataFrame(results)

    # Construire la colonne "Dirigeant" si les données sont présentes
    if "dirigeant_prenom" in df.columns or "dirigeant_nom" in df.columns:
        prenoms = df.get("dirigeant_prenom", "").fillna("") if "dirigeant_prenom" in df.columns else ""
        noms = df.get("dirigeant_nom", "").fillna("") if "dirigeant_nom" in df.columns else ""
        df["Dirigeant"] = [
            ("{} {}".format(p, n)).strip().title() if (p or n) else ""
            for p, n in zip(prenoms, noms)
        ]

    # Une seule colonne "Email" : meilleur email de prospection pondéré
    # par (confiance, source). Voir pick_best_email plus haut.
    df["Email"] = [_format_best_email(*pick_best_email(ent)) for ent in results]

    column_map = {
        "nom": "Nom",
        "adresse": "Adresse",
        "telephone": "T\u00e9l\u00e9phone",
        "site_web": "Site Web",
        "note": "Note",
        "nb_avis": "Nb avis",
        "score": "Score",
    }
    df = df.rename(columns=column_map)

    display_cols = ["Nom", "Dirigeant", "Email", "Adresse", "T\u00e9l\u00e9phone", "Site Web", "Note", "Nb avis", "Score"]
    display_cols = [c for c in display_cols if c in df.columns]
    df = df[display_cols]

    # Construire les colonnes enrichies
    df_display = df.copy()

    if show_deja_connue and "deja_connue" in pd.DataFrame(results).columns:
        deja_flags = pd.DataFrame(results)["deja_connue"].fillna(False).tolist()
        df_display["Nom"] = [
            _make_nom_link(nom, deja) for nom, deja in zip(df["Nom"], deja_flags)
        ]
    else:
        df_display["Nom"] = df["Nom"].apply(lambda n: _make_nom_link(n))

    if "Site Web" in df_display.columns:
        df_display["Site Web"] = df_display["Site Web"].apply(_make_site_link)

    if "Score" in df_display.columns:
        df_display["Score"] = df_display["Score"].apply(_make_score_badge)

    html_table = df_display.to_html(escape=False, index=False)

    # Rendre les colonnes Note, Nb avis, Score triables
    sortable_cols = {"Note": "num", "Nb avis": "num", "Score": "num"}
    for col_name, sort_type in sortable_cols.items():
        html_table = html_table.replace(
            f"<th>{col_name}</th>",
            f'<th class="sortable" data-sort="{sort_type}">{col_name}</th>',
        )

    # JavaScript de tri client-side
    sort_script = """
<script>
(function() {
    const tables = document.querySelectorAll('table');
    const table = tables[tables.length - 1];
    if (!table) return;
    table.querySelectorAll('th.sortable').forEach(th => {
        th.addEventListener('click', function() {
            const idx = Array.from(th.parentNode.children).indexOf(th);
            const tbody = table.querySelector('tbody') || table;
            const rows = Array.from(tbody.querySelectorAll('tr')).filter(r => r.querySelector('td'));
            const isAsc = th.classList.contains('sort-asc');
            table.querySelectorAll('th.sortable').forEach(h => h.classList.remove('sort-asc', 'sort-desc'));
            th.classList.add(isAsc ? 'sort-desc' : 'sort-asc');
            const dir = isAsc ? -1 : 1;
            rows.sort((a, b) => {
                let va = a.children[idx] ? a.children[idx].textContent.trim() : '';
                let vb = b.children[idx] ? b.children[idx].textContent.trim() : '';
                let na = parseFloat(va.replace(',', '.').replace(/[^0-9.-]/g, '')) || 0;
                let nb = parseFloat(vb.replace(',', '.').replace(/[^0-9.-]/g, '')) || 0;
                return (na - nb) * dir;
            });
            rows.forEach(r => tbody.appendChild(r));
        });
    });
})();
</script>
"""

    st.markdown(html_table + sort_script, unsafe_allow_html=True)

    return df


# --- Onglets ---
tab_recherche, tab_jobs, tab_historique = st.tabs(["Recherche", "Jobs", "Historique"])


# ===================== ONGLET RECHERCHE =====================
with tab_recherche:
    # --- Formulaire ---
    # --- Mode de recherche (en premier pour conditionner le reste) ---
    mode_labels = {
        "simple": "Recherche simple (~2s)",
        "approfondie": "Recherche approfondie (~15s, ~500 r\u00e9sultats)",
        "france": "France enti\u00e8re (~3-5 min, milliers de r\u00e9sultats)",
    }
    col_mode, col_advanced = st.columns([2, 2])
    with col_mode:
        mode_recherche = st.selectbox(
            "Mode de scraping",
            options=list(mode_labels.keys()),
            format_func=lambda k: mode_labels[k],
        )
    with col_advanced:
        pplx_available = perplexity_search.is_available()
        enable_advanced = st.checkbox(
            "Enrichissement avancé (Perplexity + emails + dirigeants)",
            value=False,
            disabled=not pplx_available,
            help=(
                "Active TOUT : scraping emails, API gouv (dirigeants), Perplexity Sonar "
                "(site officiel + email dirigeant), validation SMTP, emails stratégiques. "
                "~$0.0075 / entreprise enrichie. Désactivé = scraping simple sans email."
                if pplx_available else
                "PERPLEXITY_API_KEY non configurée — activer dans .env"
            ),
        )

    # Variables dérivées (pas de toggles séparés : tout est piloté par enable_advanced)
    search_emails = enable_advanced
    search_dirigeants = enable_advanced

    col1, col2, col3 = st.columns([2, 2, 1])
    with col1:
        activite = st.text_input("Domaine d'activit\u00e9", placeholder="Ex: Restaurant, Plombier, Coiffeur...")
    with col2:
        if mode_recherche == "france":
            st.text_input(
                "Zone g\u00e9ographique",
                value="France enti\u00e8re",
                disabled=True,
            )
            zone_selected = None
            zones_selected = []
        elif mode_recherche == "approfondie" and _COMMUNES_LABELS is not None:
            zones_selected = st.multiselect(
                "Zones g\u00e9ographiques",
                options=_COMMUNES_LABELS,
                placeholder="Tapez une ou plusieurs villes...",
            )
            zone_selected = zones_selected[0] if len(zones_selected) == 1 else None
        elif _COMMUNES_LABELS is not None:
            zone_selected = st.selectbox(
                "Zone g\u00e9ographique",
                options=_COMMUNES_LABELS,
                index=None,
                placeholder="Tapez une ville ou un code postal...",
            )
            zones_selected = [zone_selected] if zone_selected else []
        else:
            zone_selected = st.text_input(
                "Zone g\u00e9ographique", placeholder="Ex: Lyon, Bordeaux..."
            )
            zones_selected = [zone_selected] if zone_selected else []

    # Extraire ville, code postal et coordonn\u00e9es GPS de la s\u00e9lection
    zone = ""
    code_postal = ""
    geo_lat = None
    geo_lng = None
    if zone_selected:
        m = re.match(r"^(.+?)\s*\((\d{5})\)$", zone_selected)
        if m:
            zone = m.group(1)
            code_postal = m.group(2)
        else:
            zone = zone_selected
        coords = _COMMUNES_INDEX.get(zone_selected)
        if coords:
            geo_lat, geo_lng = coords

    with col3:
        if mode_recherche == "simple":
            max_results = st.number_input("Nb max r\u00e9sultats", min_value=5, max_value=500, value=20, step=5)
        else:
            st.markdown("**Nb r\u00e9sultats**")
            st.caption("Tous (pas de limite)")
            max_results = 999999

    # --- Paramètres avancés ---
    with st.expander("Filtres et options"):
        st.caption(
            "Ces filtres s'appliquent au scraping initial (ils excluent les "
            "r\u00e9sultats Google Maps qui n'ont pas l'info). Ils ne suppriment "
            "rien apr\u00e8s enrichissement."
        )
        col_f3, col_f4, col_f5 = st.columns(3)
        with col_f3:
            telephone_requis = st.checkbox("Uniquement avec t\u00e9l\u00e9phone")
        with col_f4:
            portable_uniquement = st.checkbox("Portable uniquement (06/07)",
                                              help="Ne garder que les num\u00e9ros de t\u00e9l\u00e9phone portable (06 ou 07)")
        with col_f5:
            site_web_requis = st.checkbox("Uniquement avec site web")
        # email_requis retir\u00e9 : c'est un filtre d'affichage, pas de pipeline.
        # L'utilisateur peut filtrer la table de r\u00e9sultats lui-m\u00eame.
        email_requis = False

        if enable_advanced:
            advanced_max = st.slider(
                "Limite d'enrichissement Perplexity (top N par score)",
                min_value=10, max_value=500, value=100, step=10,
                help="Cap le nombre d'entreprises enrichies pour contr\u00f4ler le co\u00fbt.",
            )
            cost = round(advanced_max * 0.0075, 2)
            st.caption(
                "Co\u00fbt estim\u00e9 : ~{} USD par recherche ({} entreprises \u00d7 0,0075 USD).".format(
                    "{:.2f}".format(cost), advanced_max,
                )
            )
        else:
            advanced_max = 0

    # Filtres avis Google retir\u00e9s (jamais utilis\u00e9s en pratique)
    note_minimum = 0.0
    nb_avis_minimum = 0

    st.markdown("")  # petit espace

    if st.button("Lancer la recherche", type="primary", use_container_width=True):
        if not activite or (not zones_selected and mode_recherche != "france"):
            st.warning("Veuillez remplir le domaine d'activit\u00e9" + (" et la zone." if mode_recherche != "france" else "."))
        else:
            # Construire le label de zone (multi-villes ou unique)
            if mode_recherche == "france":
                zone_label = "France enti\u00e8re"
            elif len(zones_selected) > 1:
                zone_label = ", ".join(zones_selected)
            else:
                zone_label = zone_selected if zone_selected else zone

            # L'enrichissement avancé exige les emails + dirigeants en amont
            # (Perplexity a besoin du nom pour générer les patterns email).
            # On force ces étapes à True quand l'avancé est coché. enrich_nominative
            # est désactivé sous l'avancé : redondant avec find_dirigeant_email.
            effective_search_emails = bool(search_emails or enable_advanced)
            effective_search_dirigeants = bool(search_dirigeants or enable_advanced)
            effective_enrich_nominative = bool(search_dirigeants and not enable_advanced)

            job_params = {
                "max_results": int(max_results),
                "note_minimum": float(note_minimum),
                "nb_avis_minimum": int(nb_avis_minimum),
                "telephone_requis": bool(telephone_requis),
                "portable_uniquement": bool(portable_uniquement),
                "site_web_requis": bool(site_web_requis),
                "email_requis": bool(email_requis),
                "search_emails": effective_search_emails,
                "validate_emails": effective_search_emails,
                "search_dirigeants": effective_search_dirigeants,
                "enrich_nominative": effective_enrich_nominative,
                "enrich_advanced": bool(enable_advanced),
                "do_perplexity": bool(enable_advanced),
                "do_strategic": bool(enable_advanced),
                "advanced_max": int(advanced_max) if enable_advanced else None,
                "code_postal": code_postal,
                "geo_lat": geo_lat,
                "geo_lng": geo_lng,
                "mode": mode_recherche,
                "zones_selected": zones_selected,
            }

            job_id = create_job(activite, zone_label, job_params)
            st.success(
                "Job #{} cr\u00e9\u00e9 : \u00ab {} \u00bb \u00e0 {}. Suivi dans l'onglet **Jobs**.".format(
                    job_id, activite, zone_label,
                )
            )
            st.session_state["last_job_id"] = job_id
            # Le bloc legacy ci-dessous n'est plus ex\u00e9cut\u00e9 (plac\u00e9 sous 'if False'
            # pour conserver le code de r\u00e9f\u00e9rence le temps d'une session).
        if False:
            progress_bar = st.progress(0)
            status_text = st.empty()
            error_container = {"message": None}

            def update_progress(message, progress):
                status_text.text(message)
                progress_bar.progress(min(progress, 1.0))

            def on_error(msg):
                error_container["message"] = msg

            # Multi-villes en mode approfondie
            if mode_recherche == "approfondie" and len(zones_selected) > 1:
                all_results = []
                seen_names = set()
                total_zones = len(zones_selected)

                with st.spinner("Recherche en cours..."):
                    for zi, zs in enumerate(zones_selected):
                        # Extraire infos de cette zone
                        z_m = re.match(r"^(.+?)\s*\((\d{5})\)$", zs)
                        z_zone = z_m.group(1) if z_m else zs
                        z_cp = z_m.group(2) if z_m else ""
                        z_coords = _COMMUNES_INDEX.get(zs)
                        z_lat = z_coords[0] if z_coords else None
                        z_lng = z_coords[1] if z_coords else None

                        def _city_progress(message, progress):
                            overall = (zi + progress) / total_zones
                            status_text.text("{}/{} — {} — {}".format(
                                zi + 1, total_zones, z_zone, message,
                            ))
                            progress_bar.progress(min(overall, 0.99))

                        batch = scrape_google_maps(
                            activite,
                            z_zone,
                            max_results=max_results,
                            note_minimum=note_minimum,
                            nb_avis_minimum=nb_avis_minimum,
                            telephone_requis=telephone_requis,
                            portable_uniquement=portable_uniquement,
                            site_web_requis=site_web_requis,
                            code_postal=z_cp,
                            geo_lat=z_lat,
                            geo_lng=z_lng,
                            mode=mode_recherche,
                            progress_callback=_city_progress,
                            error_callback=on_error,
                        )

                        if batch:
                            for biz in batch:
                                if biz["nom"] not in seen_names:
                                    seen_names.add(biz["nom"])
                                    all_results.append(biz)

                results = all_results
            else:
                with st.spinner("Recherche en cours..."):
                    results = scrape_google_maps(
                        activite,
                        zone,
                        max_results=max_results,
                        note_minimum=note_minimum,
                        nb_avis_minimum=nb_avis_minimum,
                        telephone_requis=telephone_requis,
                        portable_uniquement=portable_uniquement,
                        site_web_requis=site_web_requis,
                        code_postal=code_postal,
                        geo_lat=geo_lat,
                        geo_lng=geo_lng,
                        mode=mode_recherche,
                        progress_callback=update_progress,
                        error_callback=on_error,
                    )

            # Afficher l'erreur Google si détectée
            if error_container["message"]:
                st.error(error_container["message"])

            if not results:
                if not error_container["message"]:
                    st.warning("Aucun r\u00e9sultat trouv\u00e9. Essayez avec d'autres termes de recherche.")
            else:
                if search_emails:
                    status_text.text("Recherche des emails...")
                    progress_bar.progress(0.0)
                    results = enrich_emails(results, progress_callback=update_progress)

                    status_text.text("Validation SMTP des emails...")
                    progress_bar.progress(0.0)
                    results = validate_scraped_emails(results, progress_callback=update_progress)

                if email_requis:
                    results = [r for r in results if r.get("emails")]

                if search_dirigeants:
                    status_text.text("Recherche des dirigeants...")
                    progress_bar.progress(0.0)
                    results = enrich_entreprises(results, progress_callback=update_progress)

                    # Génération + validation d'emails nominatifs du dirigeant
                    status_text.text("Recherche emails dirigeants...")
                    progress_bar.progress(0.0)
                    results = enrich_nominative_emails(results, progress_callback=update_progress)

                # Scoring
                results = calculate_scores(results)

                # Sauvegarder en base
                if len(zones_selected) > 1:
                    zone_label = ", ".join(zones_selected)
                else:
                    zone_label = zone_selected if zone_selected else zone
                save_search(
                    activite, zone_label,
                    {
                        "note_minimum": note_minimum,
                        "nb_avis_minimum": nb_avis_minimum,
                        "telephone_requis": telephone_requis,
                        "portable_uniquement": portable_uniquement,
                        "site_web_requis": site_web_requis,
                        "email_requis": email_requis,
                    },
                    results,
                )

                # Trier par score décroissant
                results.sort(key=lambda r: r.get("score", 0), reverse=True)

                progress_bar.progress(1.0)
                status_text.text("{} entreprises trouv\u00e9es".format(len(results)))

                st.session_state["results"] = results
                st.session_state["search_activite"] = activite
                st.session_state["search_zone"] = zone_label

    # --- Résultats du dernier job complété (rechargés depuis l'historique) ---
    last_job_id = st.session_state.get("last_job_id")
    if last_job_id:
        last_job = get_job(last_job_id)
        if last_job and last_job.get("status") == "done" and last_job.get("search_id"):
            sid = last_job["search_id"]
            if st.session_state.get("loaded_search_id") != sid:
                st.session_state["results"] = get_search_results(sid)
                st.session_state["search_activite"] = last_job.get("activite", "")
                st.session_state["search_zone"] = last_job.get("zone", "")
                st.session_state["loaded_search_id"] = sid

    if "results" in st.session_state and st.session_state["results"]:
        results = st.session_state["results"]

        st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

        df = render_results_table(results, show_deja_connue=True)

        # Export
        col_excel, col_airtable = st.columns(2)

        df_raw = _build_export_df(results)

        with col_excel:
            df_excel = df_raw.copy()
            df_excel["Statut"] = ""
            df_excel["Date d'envoi"] = ""
            df_excel["Nom nettoyé"] = ""
            df_excel["Ville"] = ""
            st.download_button(
                label="Télécharger Excel",
                data=_export_excel(df_excel),
                file_name="prospection.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

        with col_airtable:
            df_at = df_raw.copy()
            if "Score" in df_at.columns:
                df_at["Score Label"] = df_at["Score"].apply(
                    lambda s: score_label(int(s)) if pd.notna(s) and s != "" else ""
                )
            search_activite = st.session_state.get("search_activite", "")
            search_zone = st.session_state.get("search_zone", "")
            df_at["Search Query"] = "{} à {}".format(search_activite, search_zone)
            df_at["Date Found"] = datetime.now().strftime("%Y-%m-%d")
            df_at["Statut"] = ""
            df_at["Date d'envoi"] = ""
            df_at["Nom nettoyé"] = ""
            df_at["Ville"] = ""
            for col in df_at.select_dtypes(include="object").columns:
                df_at[col] = df_at[col].fillna("").astype(str).str.strip()
            csv_airtable = "\ufeff" + df_at.to_csv(index=False)
            st.download_button(
                label="Export Airtable (CSV)",
                data=csv_airtable.encode("utf-8"),
                file_name="prospection_airtable.csv",
                mime="text/csv",
                use_container_width=True,
            )


# ===================== ONGLET JOBS =====================
with tab_jobs:
    st.markdown("### Jobs en cours et récents")
    st.caption(
        "Le worker exécute les recherches en arrière-plan. Tu peux fermer le "
        "navigateur, le job continuera et tu retrouveras les résultats ici à ton retour."
    )

    # Auto-refresh tant qu'au moins un job est pending/running
    _running_jobs = list_jobs(limit=10, status_in=["pending", "running"])
    if _running_jobs and st_autorefresh is not None:
        st_autorefresh(interval=3000, key="jobs_refresh")

    col_filter, col_refresh = st.columns([3, 1])
    with col_filter:
        statut_filtre = st.multiselect(
            "Filtre statuts",
            options=["pending", "running", "done", "failed", "cancelled"],
            default=["pending", "running", "done"],
        )
    with col_refresh:
        if st.button("Rafraîchir", use_container_width=True):
            st.rerun()

    jobs = list_jobs(limit=50, status_in=statut_filtre or None)
    if not jobs:
        st.info("Aucun job. Lance une recherche depuis l'onglet **Recherche**.")
    else:
        for j in jobs:
            status = j["status"]
            badge = {
                "pending":   "🟡 En attente",
                "running":   "🔵 En cours",
                "done":      "🟢 Terminé",
                "failed":    "🔴 Échec",
                "cancelled": "⚪ Annulé",
            }.get(status, status)

            with st.container():
                col_main, col_act = st.columns([5, 1])
                with col_main:
                    st.markdown(
                        "**#{} — {} → {}**  · {}".format(
                            j["id"], j["activite"], j["zone"], badge,
                        )
                    )
                    if status == "running":
                        st.progress(min(float(j.get("progress") or 0.0), 1.0))
                        st.caption(j.get("message") or "...")
                    elif status == "pending":
                        st.caption(
                            "En file d'attente depuis {} (worker pas encore disponible).".format(
                                j.get("created_at") or "?"
                            )
                        )
                    elif status == "done":
                        st.caption(
                            "Terminé · {} entreprise(s) · {}".format(
                                j.get("results_count") or 0, j.get("finished_at") or "",
                            )
                        )
                        # Détails par étape (si stats stockées)
                        stats_raw = j.get("stats_json") or ""
                        if stats_raw:
                            try:
                                _stats = json.loads(stats_raw)
                            except Exception:
                                _stats = {}
                            if _stats:
                                bits = []
                                if "scraping" in _stats:
                                    bits.append("scraping {}".format(_stats["scraping"]))
                                if "with_email_after_scraping" in _stats:
                                    bits.append("emails site {}".format(_stats["with_email_after_scraping"]))
                                if "valid_emails" in _stats:
                                    bits.append("SMTP valid {}".format(_stats["valid_emails"]))
                                if "dirigeants_trouves" in _stats:
                                    bits.append("dirigeants {}".format(_stats["dirigeants_trouves"]))
                                if "email_dirigeant_advanced" in _stats:
                                    bits.append("emails dir advanced {}".format(_stats["email_dirigeant_advanced"]))
                                if "with_strategic" in _stats:
                                    bits.append("stratégiques {}".format(_stats["with_strategic"]))
                                if "with_any_email" in _stats:
                                    bits.append("**total avec email {}**".format(_stats["with_any_email"]))
                                if bits:
                                    st.caption(" · ".join(bits))
                    elif status == "failed":
                        st.error("Échec : " + (j.get("error") or "raison inconnue")[:300])
                    elif status == "cancelled":
                        st.caption("Annulé : " + (j.get("finished_at") or ""))

                with col_act:
                    if status == "done" and j.get("search_id"):
                        if st.button("Voir", key="view_{}".format(j["id"])):
                            st.session_state["last_job_id"] = j["id"]
                            st.session_state["loaded_search_id"] = None  # force reload
                            st.success("Résultats chargés. Va sur l'onglet Recherche.")
                    if status == "pending":
                        if st.button("Annuler", key="cancel_{}".format(j["id"])):
                            cancel_job(j["id"])
                            st.rerun()
                    if status in ("done", "failed", "cancelled"):
                        if st.button("Suppr.", key="del_{}".format(j["id"])):
                            delete_job(j["id"])
                            st.rerun()
            st.markdown("---")


# ===================== ONGLET HISTORIQUE =====================
with tab_historique:
    st.markdown("### Historique des recherches")

    searches = get_searches()

    if not searches:
        st.info("Aucune recherche enregistr\u00e9e. Lancez une recherche pour commencer.")
    else:
        for s in searches:
            date_str = s["date_recherche"][:16]
            label = f"{s['activite']} \u00e0 {s['zone']} \u2014 {s['nb_resultats']} r\u00e9sultats \u2014 {date_str}"

            with st.expander(label):
                col_info, col_email, col_del = st.columns([6, 2, 2])
                with col_email:
                    if st.button("Rechercher les emails", key=f"email_{s['id']}"):
                        st.session_state[f"run_email_{s['id']}"] = True
                with col_del:
                    if st.button("Supprimer", key=f"del_{s['id']}"):
                        delete_search(s["id"])
                        st.rerun()

                # Lancer l'enrichissement email si demandé
                if st.session_state.get(f"run_email_{s['id']}"):
                    del st.session_state[f"run_email_{s['id']}"]
                    hist_data = get_search_results(s["id"])
                    sites_count = sum(1 for e in hist_data if e.get("site_web"))
                    progress_bar = st.progress(0)
                    status_text = st.empty()

                    def _update_progress(message, progress):
                        status_text.text(message)
                        progress_bar.progress(min(progress, 1.0))

                    hist_data = enrich_emails(hist_data, progress_callback=_update_progress)
                    hist_data = validate_scraped_emails(hist_data, progress_callback=_update_progress)
                    hist_data = calculate_scores(hist_data)
                    update_entreprises(hist_data)
                    progress_bar.progress(1.0)
                    emails_found = sum(1 for e in hist_data if e.get("emails"))
                    status_text.text(f"{emails_found} emails trouves sur {sites_count} sites")
                    st.rerun()

                hist_results = get_search_results(s["id"])
                if hist_results:
                    hist_df = render_results_table(hist_results)

                    # Boutons d'export
                    col_exp_excel, col_exp_airtable = st.columns(2)

                    df_raw = _build_export_df(hist_results)

                    with col_exp_excel:
                        df_excel = df_raw.copy()
                        df_excel["Statut"] = ""
                        df_excel["Date d'envoi"] = ""
                        df_excel["Nom nettoyé"] = ""
                        df_excel["Ville"] = ""
                        st.download_button(
                            label="Télécharger Excel",
                            data=_export_excel(df_excel),
                            file_name="prospection_%s_%s.xlsx" % (s['activite'], s['zone']),
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            use_container_width=True,
                            key="excel_%s" % s['id'],
                        )

                    with col_exp_airtable:
                        df_at = df_raw.copy()
                        if "Score" in df_at.columns:
                            df_at["Score Label"] = df_at["Score"].apply(
                                lambda sc: score_label(int(sc)) if pd.notna(sc) and sc != "" else ""
                            )
                        df_at["Search Query"] = "%s à %s" % (s['activite'], s['zone'])
                        df_at["Date Found"] = s["date_recherche"][:10]
                        df_at["Statut"] = ""
                        df_at["Date d'envoi"] = ""
                        df_at["Nom nettoyé"] = ""
                        df_at["Ville"] = ""
                        for col in df_at.select_dtypes(include="object").columns:
                            df_at[col] = df_at[col].fillna("").astype(str).str.strip()
                        csv_at = "\ufeff" + df_at.to_csv(index=False)
                        st.download_button(
                            label="Export Airtable (CSV)",
                            data=csv_at.encode("utf-8"),
                            file_name="prospection_%s_%s_airtable.csv" % (s['activite'], s['zone']),
                            mime="text/csv",
                            use_container_width=True,
                            key="airtable_%s" % s['id'],
                        )
                else:
                    st.write("Aucun r\u00e9sultat pour cette recherche.")

        st.markdown("---")
        if st.button("Supprimer tout l'historique"):
            delete_all_history()
            st.rerun()
