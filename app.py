"""Interface Streamlit pour l'outil de prospection."""

import streamlit as st
import pandas as pd
import base64
from io import BytesIO
from pathlib import Path
from urllib.parse import quote_plus

from scraper import scrape_google_maps
from email_enricher import enrich_emails

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
        border-collapse: collapse;
        font-size: 0.9rem;
    }
    table th {
        background: #0EA5E9;
        color: #fff;
        padding: 10px 12px;
        text-align: left;
        font-weight: 600;
    }
    table td {
        padding: 8px 12px;
        border-bottom: 1px solid #e2e8f0;
    }
    table tr:hover td {
        background: #f0f9ff;
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

# --- Formulaire ---
col1, col2, col3 = st.columns([2, 2, 1])
with col1:
    activite = st.text_input("Domaine d'activit\u00e9", placeholder="Ex: Restaurant, Plombier, Coiffeur...")
with col2:
    zone = st.text_input("Zone g\u00e9ographique", placeholder="Ex: Lyon, Paris 15e, Bordeaux...")
with col3:
    max_results = st.number_input("Nb max r\u00e9sultats", min_value=5, max_value=100, value=20, step=5)

# --- Param\u00e8tres avanc\u00e9s ---
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

search_emails = st.checkbox("Rechercher les emails (plus lent)", value=True)

st.markdown("")  # petit espace

if st.button("Rechercher", type="primary", use_container_width=True):
    if not activite or not zone:
        st.warning("Veuillez remplir les deux champs.")
    else:
        progress_bar = st.progress(0)
        status_text = st.empty()

        def update_progress(message, progress):
            status_text.text(message)
            progress_bar.progress(min(progress, 1.0))

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
                progress_callback=update_progress,
            )

        if not results:
            st.error("Aucun r\u00e9sultat trouv\u00e9. Essayez avec d'autres termes de recherche.")
        else:
            if search_emails:
                status_text.text("Recherche des emails...")
                progress_bar.progress(0.0)
                results = enrich_emails(results, progress_callback=update_progress)

            if email_requis:
                results = [r for r in results if r.get("emails")]

            progress_bar.progress(1.0)
            status_text.text(f"{len(results)} entreprises trouv\u00e9es")

            st.session_state["results"] = results

# --- R\u00e9sultats ---
if "results" in st.session_state and st.session_state["results"]:
    results = st.session_state["results"]

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    df = pd.DataFrame(results)

    column_map = {
        "nom": "Nom",
        "adresse": "Adresse",
        "telephone": "T\u00e9l\u00e9phone",
        "site_web": "Site Web",
        "emails": "Email",
        "note": "Note",
        "nb_avis": "Nb avis",
    }
    df = df.rename(columns=column_map)

    display_cols = ["Nom", "Adresse", "T\u00e9l\u00e9phone", "Email", "Site Web", "Note", "Nb avis"]
    display_cols = [c for c in display_cols if c in df.columns]
    df = df[display_cols]

    # Nom -> lien Google, Site Web -> lien cliquable
    def _make_nom_link(nom):
        if not nom:
            return ""
        url = f"https://www.google.com/search?q={quote_plus(str(nom))}"
        return f'<a href="{url}" target="_blank" style="color:#0EA5E9;text-decoration:none;">{nom}</a>'

    def _make_site_link(site):
        if not site:
            return ""
        href = site if site.startswith("http") else f"https://{site}"
        return f'<a href="{href}" target="_blank" style="color:#0EA5E9;text-decoration:none;">{site}</a>'

    df_display = df.copy()
    df_display["Nom"] = df_display["Nom"].apply(_make_nom_link)
    if "Site Web" in df_display.columns:
        df_display["Site Web"] = df_display["Site Web"].apply(_make_site_link)

    st.markdown(df_display.to_html(escape=False, index=False), unsafe_allow_html=True)

    # Export
    col_csv, col_excel = st.columns(2)

    with col_csv:
        csv_data = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            label="T\u00e9l\u00e9charger CSV",
            data=csv_data,
            file_name="prospection.csv",
            mime="text/csv",
            use_container_width=True,
        )

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
