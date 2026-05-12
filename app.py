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
from email_processor import (
    PUBLIC_EMAIL_DOMAINS,
    pick_best_email as _pick_best_email_dict,
    classify_tier,
    assign_destinataire_ranks,
    tier_label,
    tier_color,
)
import pipeline as pipeline_module
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
    table th:nth-child(1), table td:nth-child(1) { width: 13%; }  /* Nom */
    table th:nth-child(2), table td:nth-child(2) { width: 11%; }  /* Dirigeant */
    table th:nth-child(3), table td:nth-child(3) { width: 18%; }  /* Email */
    table th:nth-child(4), table td:nth-child(4) { width: 8%; text-align: center; }  /* Source */
    table th:nth-child(5), table td:nth-child(5) { width: 8%; text-align: center; }  /* Qualité */
    table th:nth-child(6), table td:nth-child(6) { width: 14%; }  /* Adresse */
    table th:nth-child(7), table td:nth-child(7) { width: 9%; }   /* Téléphone */
    table th:nth-child(8), table td:nth-child(8) { width: 9%; }   /* Site Web */
    table th:nth-child(9), table td:nth-child(9) { width: 10%; text-align: center; } /* Score */
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
# Libellés par rang destinataire (1..7), cf. email_processor.assign_destinataire_ranks
_RANK_LABEL = {
    1: "Dirigeant",
    2: "Direction",
    3: "Perso (solo)",
    4: "Contact",
    5: "Perso",
    6: "Commercial",
    7: "RH",
}

_STATUS_LABEL = {
    "valid": "vérifié",
    "catchall": "catchall",
    "unknown": "incertain",
    "": "non testé",
    "error": "non testé",
}
_STATUS_COLOR = {
    "valid": "#10b981", "catchall": "#f59e0b",
    "unknown": "#94a3b8", "": "#94a3b8", "error": "#94a3b8",
}


def best_email_view(entreprise):
    """Retourne un dict pratique pour l'UI :
        {email, source, qualite, qualite_color, rank, is_public, smtp_status}
    Toutes valeurs vides si pas d'email."""
    best = _pick_best_email_dict(entreprise)
    if best is None:
        return {
            "email": "", "source": "", "qualite": "",
            "qualite_color": "#94a3b8", "rank": 0,
            "is_public": False, "smtp_status": "",
        }
    rank = int(best.get("destinataire_rank") or 4)
    status = (best.get("smtp_status") or "").lower()
    is_public = bool(best.get("is_public_domain"))
    qualite = "perso" if is_public else _STATUS_LABEL.get(status, status or "non testé")
    return {
        "email": best.get("email", ""),
        "source": _RANK_LABEL.get(rank, "Contact"),
        "qualite": qualite,
        "qualite_color": _STATUS_COLOR.get(status, "#94a3b8"),
        "rank": rank,
        "is_public": is_public,
        "smtp_status": status,
    }


def dedup_by_best_email(results):
    """Dédoublonne par email principal. Sans email = jamais dédupliqué.
    Garde l'entreprise au score le plus élevé."""
    ordered = sorted(results, key=lambda r: r.get("score", 0), reverse=True)
    seen, deduped = set(), []
    for r in ordered:
        email = best_email_view(r)["email"].strip().lower()
        if email and email in seen:
            continue
        if email:
            seen.add(email)
        deduped.append(r)
    return deduped


def _format_email_link(email):
    """Lien mailto stylisé, ou chaîne vide."""
    if not email:
        return ""
    return ('<a href="mailto:' + email + '" style="color:#0EA5E9;'
            'text-decoration:none;">' + email + "</a>")


def _qualite_badge_from_view(view):
    """Badge coloré pour la colonne Qualité."""
    if not view.get("email"):
        return ""
    color = "#94a3b8" if view.get("is_public") else view["qualite_color"]
    return ('<span style="background:' + color + ';color:#fff;padding:2px 8px;'
            'border-radius:8px;font-size:0.72rem;">' + view["qualite"] + "</span>")


def _tier_badge(tier):
    """Badge coloré du tier P0/P1/P2/X."""
    if not tier:
        return ""
    return ('<span style="background:' + tier_color(tier) + ';color:#fff;padding:2px 8px;'
            'border-radius:8px;font-size:0.72rem;font-weight:600;">' + tier_label(tier) + "</span>")






def _build_export_df(results, tier_filter=None):
    """DataFrame pour l'export Excel. tier_filter limite aux tiers donnes.

    Colonnes : Nom | Adresse | Telephone | Email | Source Email |
    Qualite Email | Site Web | Dirigeant | Statut | Date d'envoi |
    Nom nettoye | Ville | Note
    """
    if tier_filter:
        results = [r for r in results if r.get("tier") in tier_filter]
    df = pd.DataFrame(results)
    views = [best_email_view(ent) for ent in results]
    df["Email"] = [v["email"] for v in views]
    df["Source Email"] = [v["source"] for v in views]
    df["Qualite Email"] = [v["qualite"] for v in views]

    prenoms = df.get("dirigeant_prenom", pd.Series([""] * len(df))).fillna("")
    noms = df.get("dirigeant_nom", pd.Series([""] * len(df))).fillna("")
    df["Dirigeant"] = [
        ("{} {}".format(p, n)).strip().title() if (p or n) else ""
        for p, n in zip(prenoms, noms)
    ]

    from scoring import calculate_score
    df["Note"] = [calculate_score(ent) for ent in results]

    rename = {
        "nom": "Nom",
        "adresse": "Adresse",
        "telephone": "Telephone",
        "site_web": "Site Web",
    }
    df = df.rename(columns=rename)
    df["Statut"] = ""
    df["Date d'envoi"] = ""
    df["Nom nettoye"] = ""
    df["Ville"] = ""

    cols = ["Nom", "Adresse", "Telephone", "Email", "Source Email",
            "Qualite Email", "Site Web", "Dirigeant",
            "Statut", "Date d'envoi", "Nom nettoye", "Ville", "Note"]
    cols = [c for c in cols if c in df.columns]
    df = df[cols]
    return df


def _export_excel(results, tier_filter=None):
    """Excel format sortie.xlsx : Sheet1 + Envoi + Relance1 + Relance2.

    `tier_filter` (set ou None) limite Sheet1 aux tiers donnes :
      - None ou vide  -> toutes les entreprises
      - {"P0", "P1"}  -> liste import Debounce
      - {"P2"}        -> liste recherche manuelle

    La colonne Date ISO est calculee par formule a partir de la colonne
    `Date d'envoi`.
    """
    buffer = BytesIO()
    df_export = _build_export_df(results, tier_filter=tier_filter)
    df_with_iso = df_export.copy()
    df_with_iso["Date ISO"] = ""
    df_suivi = pd.DataFrame(columns=["Destinataire", "Date et Heure", "Objet", "Corp"])

    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df_with_iso.to_excel(writer, index=False, sheet_name="Sheet1")
        df_suivi.to_excel(writer, index=False, sheet_name="Envoi")
        df_suivi.to_excel(writer, index=False, sheet_name="Relance1")
        df_suivi.to_excel(writer, index=False, sheet_name="Relance2")

        if not df_export.empty:
            cols = list(df_with_iso.columns)
            try:
                envoi_idx = cols.index("Date d'envoi") + 1
                iso_idx = cols.index("Date ISO") + 1
            except ValueError:
                envoi_idx = iso_idx = None
            if envoi_idx and iso_idx and envoi_idx <= 26:
                envoi_letter = chr(ord("A") + envoi_idx - 1)
                ws = writer.sheets["Sheet1"]
                for row_num in range(2, ws.max_row + 1):
                    ws.cell(row=row_num, column=iso_idx).value = (
                        '=IF(' + envoi_letter + str(row_num) + '="","",'
                        'TEXT(DATE(VALUE(MID(' + envoi_letter + str(row_num) + ',7,4)),'
                        'VALUE(MID(' + envoi_letter + str(row_num) + ',4,2)),'
                        'VALUE(LEFT(' + envoi_letter + str(row_num) + ',2))),"YYYY-MM-DD")'
                        '&" "&MID(' + envoi_letter + str(row_num) + ',14,5))'
                    )
    return buffer.getvalue()




def render_results_table(results, show_deja_connue=False):
    """Tableau HTML des resultats avec colonnes Tier, Source, Qualite, Email."""
    df = pd.DataFrame(results)

    if "dirigeant_prenom" in df.columns or "dirigeant_nom" in df.columns:
        prenoms = df.get("dirigeant_prenom", "").fillna("") if "dirigeant_prenom" in df.columns else ""
        noms = df.get("dirigeant_nom", "").fillna("") if "dirigeant_nom" in df.columns else ""
        df["Dirigeant"] = [
            ("{} {}".format(p, n)).strip().title() if (p or n) else ""
            for p, n in zip(prenoms, noms)
        ]

    views = [best_email_view(ent) for ent in results]
    df["Email"] = [_format_email_link(v["email"]) for v in views]
    df["Source"] = [v["source"] for v in views]
    df["Qualite"] = [_qualite_badge_from_view(v) for v in views]
    df["Tier"] = [_tier_badge(ent.get("tier") or "") for ent in results]

    column_map = {
        "nom": "Nom",
        "adresse": "Adresse",
        "telephone": "Telephone",
        "site_web": "Site Web",
        "score": "Score",
    }
    df = df.rename(columns=column_map)

    display_cols = ["Nom", "Tier", "Dirigeant", "Email", "Source",
                    "Qualite", "Adresse", "Telephone", "Site Web", "Score"]
    display_cols = [c for c in display_cols if c in df.columns]
    df = df[display_cols]

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

    sortable_cols = {"Score": "num"}
    for col_name, sort_type in sortable_cols.items():
        html_table = html_table.replace(
            "<th>" + col_name + "</th>",
            '<th class="sortable" data-sort="' + sort_type + '">' + col_name + '</th>',
        )

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
        "approfondie": "Recherche approfondie (~30s, ~500 r\u00e9sultats)",
        "ultra": "Ultra (grille dense ~80 points, ~3-5 min, ~1000-1500 r\u00e9sultats)",
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
            "Booster Perplexity + patterns dirigeant (payant)",
            value=False,
            disabled=not pplx_available,
            help=(
                "Par défaut, le pipeline fait déjà tout ce qui est gratuit : "
                "scraping emails sur les sites validés, API gouv (dirigeants), "
                "classification MX, validation SMTP. "
                "Cette case ajoute UNIQUEMENT : "
                "1) Perplexity Sonar pour retrouver le site officiel des entreprises sans site, "
                "2) génération + SMTP test de patterns dirigeant sur les hébergeurs modernes "
                "(Google Workspace, Microsoft 365). Sur OVH/IONOS opaques, aucun pattern n'est "
                "généré quelle que soit cette case. "
                "~$0.0075 / entreprise enrichie par Perplexity."
                if pplx_available else
                "PERPLEXITY_API_KEY non configurée — activer dans .env"
            ),
        )

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
            "T\u00e9l\u00e9phone/site = filtres GMaps appliqu\u00e9s au scraping initial. "
            "Email = filtre final appliqu\u00e9 APR\u00c8S l'enrichissement complet : "
            "toutes les entreprises passent par le pipeline, et seules celles "
            "avec un email final sont gard\u00e9es en sauvegarde."
        )
        col_f3, col_f4, col_f5, col_f6 = st.columns(4)
        with col_f3:
            telephone_requis = st.checkbox("Uniquement avec t\u00e9l\u00e9phone")
        with col_f4:
            portable_uniquement = st.checkbox("Portable uniquement (06/07)",
                                              help="Ne garder que les num\u00e9ros de t\u00e9l\u00e9phone portable (06 ou 07)")
        with col_f5:
            site_web_requis = st.checkbox("Uniquement avec site web")
        with col_f6:
            email_requis = st.checkbox(
                "Uniquement avec email",
                value=False,
                help="Filtre final, apr\u00e8s enrichissement. Toutes les entreprises "
                     "passent le pipeline ; seules celles avec un email "
                     "(scrap\u00e9, dirigeant ou strat\u00e9gique) sont sauvegard\u00e9es.",
            )

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

            # Le pipeline v2 fait toujours les étapes gratuites (emails site,
            # dirigeants gouv, MX, SMTP). La case "avancé" n'active que
            # Perplexity (site manquant) + patterns dirigeant sur MX discriminatif.
            job_params = {
                "max_results": int(max_results),
                "note_minimum": float(note_minimum),
                "nb_avis_minimum": int(nb_avis_minimum),
                "telephone_requis": bool(telephone_requis),
                "portable_uniquement": bool(portable_uniquement),
                "site_web_requis": bool(site_web_requis),
                "email_requis": bool(email_requis),
                "search_emails": True,
                "validate_emails": True,
                "search_dirigeants": True,
                "enrich_advanced": bool(enable_advanced),
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
        results = dedup_by_best_email(st.session_state["results"])

        st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

        # Bouton de téléchargement EN HAUT pour éviter de scroller 1000 lignes
        col_count, col_dl = st.columns([3, 2])
        with col_count:
            st.markdown(
                "**%d résultats**" % len(results)
                + " — triés par score décroissant (A → D)"
            )
        with col_dl:
            st.download_button(
                label="Télécharger Excel",
                data=_export_excel(results),
                file_name="prospection.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
                key="excel_recherche",
            )

        # Filtre par tier au-dessus du tableau
        all_tiers = sorted({r.get("tier") or "?" for r in results})
        tier_choice = st.multiselect(
            "Filtrer par tier",
            options=all_tiers,
            default=all_tiers,
            format_func=lambda t: tier_label(t) if t in ("P0", "P1", "P2", "X") else t,
            key="tier_filter_recherche",
        )
        filtered = [r for r in results if (r.get("tier") or "?") in tier_choice] if tier_choice else results

        render_results_table(filtered, show_deja_connue=True)


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
                                if "email_dir_phase1" in _stats:
                                    bits.append("dir patterns {}".format(_stats["email_dir_phase1"]))
                                if "email_dir_phase2" in _stats:
                                    bits.append("dir Perplexity {}".format(_stats["email_dir_phase2"]))
                                if "with_strategic_phase2" in _stats:
                                    bits.append("stratégiques {}".format(_stats["with_strategic_phase2"]))
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
                    if status in ("pending", "running"):
                        btn_label = "Annuler" if status == "pending" else "Arrêter"
                        if st.button(btn_label, key="cancel_{}".format(j["id"])):
                            cancel_job(j["id"])
                            st.success(
                                "Job #{} arrêté. Prend effet en quelques secondes.".format(j["id"])
                            )
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
        # D\u00e9dup d\u00e9fensif : si pour une raison X la liste contient des doublons,
        # on \u00e9vite la collision de key dans Streamlit.
        seen_ids = set()
        for idx, s in enumerate(searches):
            if s["id"] in seen_ids:
                continue
            seen_ids.add(s["id"])
            date_str = s["date_recherche"][:16]
            label = f"{s['activite']} \u00e0 {s['zone']} \u2014 {s['nb_resultats']} r\u00e9sultats \u2014 {date_str}"

            with st.expander(label):
                col_info, col_email, col_del = st.columns([6, 2, 2])
                with col_email:
                    if st.button("Rechercher les emails", key=f"email_{idx}_{s['id']}"):
                        st.session_state[f"run_email_{s['id']}"] = True
                with col_del:
                    if st.button("Supprimer", key=f"del_{idx}_{s['id']}"):
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
                    pipeline_module._validate_published_emails(hist_data, progress_callback=_update_progress)
                    for ent in hist_data:
                        assign_destinataire_ranks(ent)
                        ent["tier"] = classify_tier(ent)
                    hist_data = calculate_scores(hist_data)
                    update_entreprises(hist_data)
                    progress_bar.progress(1.0)
                    emails_found = sum(1 for e in hist_data if e.get("emails"))
                    status_text.text(f"{emails_found} emails trouves sur {sites_count} sites")
                    st.rerun()

                hist_results = get_search_results(s["id"])
                if hist_results:
                    hist_results = dedup_by_best_email(hist_results)
                    n_p01 = sum(1 for r in hist_results if (r.get("tier") or "") in ("P0", "P1"))
                    n_p2 = sum(1 for r in hist_results if (r.get("tier") or "") == "P2")

                    col_count, col_dl1, col_dl2 = st.columns([3, 2, 2])
                    with col_count:
                        st.markdown("**%d résultats**" % len(hist_results))
                    with col_dl1:
                        st.download_button(
                            label="Excel Debounce (P0+P1) — %d" % n_p01,
                            data=_export_excel(hist_results, tier_filter={"P0", "P1"}),
                            file_name="prospection_%s_%s_debounce.xlsx" % (s['activite'], s['zone']),
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            use_container_width=True,
                            key="excel_debounce_%d_%s" % (idx, s["id"]),
                            disabled=(n_p01 == 0),
                        )
                    with col_dl2:
                        st.download_button(
                            label="Excel Manuel (P2) — %d" % n_p2,
                            data=_export_excel(hist_results, tier_filter={"P2"}),
                            file_name="prospection_%s_%s_manuel.xlsx" % (s['activite'], s['zone']),
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            use_container_width=True,
                            key="excel_manuel_%d_%s" % (idx, s["id"]),
                            disabled=(n_p2 == 0),
                        )
                    render_results_table(hist_results)
                else:
                    st.write("Aucun r\u00e9sultat pour cette recherche.")

        st.markdown("---")
        if st.button("Supprimer tout l'historique"):
            delete_all_history()
            st.rerun()
