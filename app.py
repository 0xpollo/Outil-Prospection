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
from scoring import calculate_scores, score_color, score_label
from database import save_search, get_searches, get_search_results, delete_search, delete_all_history


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


def render_results_table(results, show_deja_connue=False):
    """Construit et affiche le tableau HTML des résultats."""
    df = pd.DataFrame(results)

    column_map = {
        "nom": "Nom",
        "adresse": "Adresse",
        "telephone": "T\u00e9l\u00e9phone",
        "site_web": "Site Web",
        "emails": "Email",
        "note": "Note",
        "nb_avis": "Nb avis",
        "score": "Score",
    }
    df = df.rename(columns=column_map)

    display_cols = ["Nom", "Adresse", "T\u00e9l\u00e9phone", "Email", "Site Web", "Note", "Nb avis", "Score"]
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
tab_recherche, tab_historique = st.tabs(["Recherche", "Historique"])


# ===================== ONGLET RECHERCHE =====================
with tab_recherche:
    # --- Formulaire ---
    # --- Mode de recherche (en premier pour conditionner le reste) ---
    mode_labels = {
        "simple": "Recherche simple (~2s)",
        "approfondie": "Recherche approfondie (~15s, ~500 r\u00e9sultats)",
        "france": "France enti\u00e8re (~3-5 min, milliers de r\u00e9sultats)",
    }
    col_mode, col_emails = st.columns(2)
    with col_mode:
        mode_recherche = st.selectbox(
            "Mode de recherche",
            options=list(mode_labels.keys()),
            format_func=lambda k: mode_labels[k],
        )
    with col_emails:
        search_emails = st.checkbox("Rechercher les emails (plus lent)", value=(mode_recherche == "simple"))

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
        elif _COMMUNES_LABELS is not None:
            zone_selected = st.selectbox(
                "Zone g\u00e9ographique",
                options=_COMMUNES_LABELS,
                index=None,
                placeholder="Tapez une ville ou un code postal...",
            )
        else:
            zone_selected = st.text_input(
                "Zone g\u00e9ographique", placeholder="Ex: Lyon, Bordeaux..."
            )

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
    with st.expander("Param\u00e8tres de recherche"):
        col_f1, col_f2 = st.columns(2)
        with col_f1:
            note_minimum = st.slider("Note Google minimum", 0.0, 5.0, 0.0, 0.5)
        with col_f2:
            nb_avis_minimum = st.number_input("Nombre d'avis minimum", min_value=0, value=0, step=5,
                                               help="Filtrer les petites structures avec peu de visibilit\u00e9")

        col_f3, col_f4, col_f5, col_f6 = st.columns(4)
        with col_f3:
            telephone_requis = st.checkbox("Uniquement avec t\u00e9l\u00e9phone")
        with col_f4:
            portable_uniquement = st.checkbox("Portable uniquement (06/07)",
                                              help="Ne garder que les num\u00e9ros de t\u00e9l\u00e9phone portable (06 ou 07)")
        with col_f5:
            site_web_requis = st.checkbox("Uniquement avec site web")
        with col_f6:
            email_requis = st.checkbox("Uniquement avec email")

    st.markdown("")  # petit espace

    if st.button("Rechercher", type="primary", use_container_width=True):
        if not activite or (not zone and mode_recherche != "france"):
            st.warning("Veuillez remplir le domaine d'activit\u00e9" + (" et la zone." if mode_recherche != "france" else "."))
        else:
            progress_bar = st.progress(0)
            status_text = st.empty()
            error_container = {"message": None}

            def update_progress(message, progress):
                status_text.text(message)
                progress_bar.progress(min(progress, 1.0))

            def on_error(msg):
                error_container["message"] = msg

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

                if email_requis:
                    results = [r for r in results if r.get("emails")]

                # Scoring
                results = calculate_scores(results)

                # Sauvegarder en base
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
                status_text.text(f"{len(results)} entreprises trouv\u00e9es")

                st.session_state["results"] = results
                st.session_state["search_activite"] = activite
                st.session_state["search_zone"] = zone_label

    # --- Résultats ---
    if "results" in st.session_state and st.session_state["results"]:
        results = st.session_state["results"]

        st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

        df = render_results_table(results, show_deja_connue=True)

        # Export
        col_excel, col_airtable = st.columns(2)

        with col_excel:
            buffer = BytesIO()
            df.to_excel(buffer, index=False, engine="openpyxl")
            st.download_button(
                label="T\u00e9l\u00e9charger Excel",
                data=buffer.getvalue(),
                file_name="prospection.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )

        with col_airtable:
            df_at = pd.DataFrame(results)
            airtable_map = {
                "nom": "Name",
                "adresse": "Address",
                "telephone": "Phone",
                "emails": "Email",
                "site_web": "Website",
                "note": "Google Rating",
                "nb_avis": "Review Count",
                "score": "Score",
            }
            df_at = df_at.rename(columns=airtable_map)

            df_at["Score Label"] = df_at["Score"].apply(
                lambda s: score_label(int(s)) if pd.notna(s) and s != "" else ""
            )
            search_activite = st.session_state.get("search_activite", "")
            search_zone = st.session_state.get("search_zone", "")
            df_at["Search Query"] = f"{search_activite} \u00e0 {search_zone}"
            df_at["Date Found"] = datetime.now().strftime("%Y-%m-%d")

            airtable_cols = [
                "Name", "Address", "Phone", "Email", "Website",
                "Google Rating", "Review Count", "Score", "Score Label",
                "Search Query", "Date Found",
            ]
            airtable_cols = [c for c in airtable_cols if c in df_at.columns]
            df_at = df_at[airtable_cols]

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
                col_info, col_del = st.columns([8, 2])
                with col_del:
                    st.markdown('<div class="hist-delete">', unsafe_allow_html=True)
                    if st.button("Supprimer", key=f"del_{s['id']}"):
                        delete_search(s["id"])
                        st.rerun()
                    st.markdown('</div>', unsafe_allow_html=True)
                hist_results = get_search_results(s["id"])
                if hist_results:
                    hist_df = render_results_table(hist_results)

                    # Boutons d'export
                    col_exp_excel, col_exp_airtable = st.columns(2)

                    with col_exp_excel:
                        buf = BytesIO()
                        hist_df.to_excel(buf, index=False, engine="openpyxl")
                        st.download_button(
                            label="T\u00e9l\u00e9charger Excel",
                            data=buf.getvalue(),
                            file_name="prospection_%s_%s.xlsx" % (s['activite'], s['zone']),
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            use_container_width=True,
                            key="excel_%s" % s['id'],
                        )

                    with col_exp_airtable:
                        df_at_h = pd.DataFrame(hist_results)
                        at_map = {
                            "nom": "Name",
                            "adresse": "Address",
                            "telephone": "Phone",
                            "emails": "Email",
                            "site_web": "Website",
                            "note": "Google Rating",
                            "nb_avis": "Review Count",
                            "score": "Score",
                        }
                        df_at_h = df_at_h.rename(columns=at_map)
                        df_at_h["Score Label"] = df_at_h["Score"].apply(
                            lambda sc: score_label(int(sc)) if pd.notna(sc) and sc != "" else ""
                        )
                        df_at_h["Search Query"] = "%s \u00e0 %s" % (s['activite'], s['zone'])
                        df_at_h["Date Found"] = s["date_recherche"][:10]

                        at_cols = [
                            "Name", "Address", "Phone", "Email", "Website",
                            "Google Rating", "Review Count", "Score", "Score Label",
                            "Search Query", "Date Found",
                        ]
                        at_cols = [c for c in at_cols if c in df_at_h.columns]
                        df_at_h = df_at_h[at_cols]

                        for col in df_at_h.select_dtypes(include="object").columns:
                            df_at_h[col] = df_at_h[col].fillna("").astype(str).str.strip()

                        csv_at_h = "\ufeff" + df_at_h.to_csv(index=False)

                        st.download_button(
                            label="Export Airtable (CSV)",
                            data=csv_at_h.encode("utf-8"),
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
