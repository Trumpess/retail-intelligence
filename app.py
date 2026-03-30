import streamlit as st
import json
import requests
import datetime
from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm, inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether, PageBreak
)
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT

# ─── PAGE CONFIG ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="UK Retail Property Intelligence",
    page_icon="🏬",
    layout="wide"
)

# ─── PASSWORD ─────────────────────────────────────────────────────────────────
PASSWORD = "retailintel2026"

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    st.title("🏬 UK Retail Property Intelligence")
    st.subheader("Please enter the access password")
    pw = st.text_input("Password", type="password")
    if st.button("Login"):
        if pw == PASSWORD:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect password")
    st.stop()

# ─── LOAD DATA ────────────────────────────────────────────────────────────────
@st.cache_data
def load_parks():
    with open("uk_retail_assets.json") as f:
        return json.load(f)

@st.cache_data
def load_ofcom():
    try:
        with open("area_data.json") as f:
            return json.load(f)
    except Exception:
        return {}

assets_data = load_parks()
ofcom_data = load_ofcom()
parks_data = assets_data  # alias for compatibility
def build_export_data(park, ofcom, companies, report_type, area_label="",
                      parks_list=None, all_ofcom=None, all_intelligence=None,
                      epc=None, flood_risk=None):
    """
    Build a structured JSON export for use in the master report app.
    all_intelligence: optional dict keyed by park id with full intelligence results
                      (ofcom, companies, epc, flood_risk, coords).
    """
    export = {
        "source_app":          "retail_intelligence",
        "report_type":         report_type,  # "park" or "area"
        "exported_at":         datetime.datetime.now().strftime("%d %b %Y %H:%M"),
        "area_label":          area_label,
        "intelligence_run":    all_intelligence is not None or epc is not None,
    }
    if report_type == "park" and park:
        export["assets"] = [{
            "name":       park.get("name", ""),
            "postcode":   park.get("postcode", ""),
            "type":       park.get("type", ""),
            "gla_sqft":   park.get("gla_sqft", 0),
            "landlord":   park.get("landlord", ""),
            "anchor_tenants": park.get("anchor_tenants", []),
            "repositioning":  park.get("repositioning", False),
            "status":     park.get("status", ""),
            "notes":      park.get("notes", ""),
            "website":    park.get("website", ""),
            "ofcom":      ofcom or {},
            "companies":  companies or [],
            "epc":        epc or {},
            "flood_risk": flood_risk or "Unknown",
        }]
    elif report_type == "area" and parks_list:
        export["assets"] = []
        for p in parks_list:
            pid   = p.get("id", p.get("postcode", ""))
            intel = (all_intelligence or {}).get(pid, {})
            ofc   = intel.get("ofcom") or (all_ofcom or {}).get(pid, {})
            export["assets"].append({
                "name":       p.get("name", ""),
                "postcode":   p.get("postcode", ""),
                "type":       p.get("type", ""),
                "gla_sqft":   p.get("gla_sqft", 0),
                "landlord":   p.get("landlord", ""),
                "anchor_tenants": p.get("anchor_tenants", []),
                "repositioning":  p.get("repositioning", False),
                "status":     p.get("status", ""),
                "notes":      p.get("notes", ""),
                "website":    p.get("website", ""),
                "ofcom":      ofc,
                "companies":  intel.get("companies", []),
                "epc":        intel.get("epc", {}),
                "flood_risk": intel.get("flood_risk", ""),
            })
    return export

# ─── FLATTEN NESTED OFCOM STRUCTURE ──────────────────────────────────────────
def flatten_ofcom(raw):
    """
    area_data.json uses nested sub-objects: connectivity, mobile, energy.
    This converts them to the flat field names the rest of the app expects.
    Returns an empty dict (falsy) if the raw entry is empty/None.
    """
    if not raw:
        return {}

    conn  = raw.get("connectivity") or {}
    mob   = raw.get("mobile") or {}
    # energy not used in scoring yet but available if needed

    flat = {
        # Connectivity
        "full_fibre_pct":        conn.get("full_fibre_pct"),
        "gigabit_pct":           conn.get("gigabit_pct"),
        "superfast_pct":         conn.get("superfast_pct"),
        "no_decent_pct":         conn.get("no_decent_pct"),
        "full_fibre_takeup_pct": conn.get("ff_takeup_pct"),   # key name differs
        "avg_data_usage_gb":     conn.get("avg_data_usage_gb"),
        # Mobile
        "indoor_4g_pct":         mob.get("indoor_4g_all_operators_pct"),
        "outdoor_4g_pct":        mob.get("outdoor_4g_all_operators_pct"),
        "outdoor_5g_pct":        mob.get("outdoor_5g_all_operators_pct"),
        "indoor_voice_pct":      mob.get("indoor_voice_all_operators_pct"),
    }

    # Guard against legacy merged-council entries that have all zeros:
    # treat them as no-data so they don't score 20/100 misleadingly.
    conn_vals = [v for v in [flat["full_fibre_pct"], flat["gigabit_pct"],
                              flat["indoor_4g_pct"], flat["outdoor_5g_pct"]] if v is not None]
    if conn_vals and all(v == 0 for v in conn_vals):
        return {}   # signal "no usable data"

    return flat

# ─── HELPERS: DATA LOOKUPS ────────────────────────────────────────────────────
def get_ofcom(local_authority):
    if not ofcom_data:
        return {}
    la_lower = local_authority.lower().strip()
    for key, val in ofcom_data.items():
        if key.lower().strip() == la_lower:
            return flatten_ofcom(val)
    # fuzzy
    for key, val in ofcom_data.items():
        if la_lower in key.lower() or key.lower() in la_lower:
            return flatten_ofcom(val)
    return {}

def get_companies(postcode, api_key, max_results=20):
    if not api_key or not postcode:
        return []
    try:
        pc = postcode.replace(" ", "+")
        url = f"https://api.company-information.service.gov.uk/search/companies?q={pc}&items_per_page={max_results}"
        r = requests.get(url, auth=(api_key, ""), timeout=8)
        if r.status_code == 200:
            data = r.json()
            return data.get("items", [])
    except Exception:
        pass
    return []

# ─── LIVE API HELPERS: EPC / FLOOD / COORDS ───────────────────────────────────
def get_postcode_coords(postcode):
    """Resolve a UK postcode to lat/lon via postcodes.io (free, no key required)."""
    if not postcode:
        return None, None
    try:
        pc = postcode.replace(" ", "")
        r = requests.get(f"https://api.postcodes.io/postcodes/{pc}", timeout=6)
        if r.status_code == 200:
            result = r.json().get("result") or {}
            return result.get("latitude"), result.get("longitude")
    except Exception:
        pass
    return None, None

def get_epc_data(postcode, epc_bearer_token):
    """
    Fetch non-domestic EPC certificates for a postcode.
    Uses GOV.UK One Login Bearer token — same API and auth as cre-intelligence app.
    """
    if not epc_bearer_token or not postcode:
        return {}
    try:
        from collections import Counter
        from datetime import datetime
        url = "https://api.get-energy-performance-data.communities.gov.uk/api/non-domestic/search"
        headers = {"Authorization": f"Bearer {epc_bearer_token}", "Accept": "application/json"}
        r = requests.get(url, params={"postcode": postcode.strip()}, headers=headers, timeout=15)
        r.raise_for_status()
        rows = r.json().get("data", [])
        if not rows:
            return {}
        ratings = [row.get("currentEnergyEfficiencyBand", "").upper() for row in rows
                   if row.get("currentEnergyEfficiencyBand")]
        if not ratings:
            return {}
        counts = Counter(ratings)
        total = len(ratings)
        abc = sum(counts.get(x, 0) for x in ["A", "B", "C"])
        return {
            "total":       total,
            "abc_pct":     round(abc / total * 100) if total else 0,
            "most_common": counts.most_common(1)[0][0] if counts else "—",
            "ratings":     dict(counts),
        }
    except Exception:
        pass
    return {}

def get_flood_risk(lat, lon):
    """
    Check EA Flood Map for Planning zones (3 then 2) for a lat/lon point.
    Returns 'Zone 3 (High)', 'Zone 2 (Medium)', or 'Zone 1 (Low)'.
    """
    if not lat or not lon:
        return "Unknown"
    base = "https://environment.maps.arcgis.com/arcgis/rest/services/EA"
    params = {
        "geometry":      f"{lon},{lat}",
        "geometryType":  "esriGeometryPoint",
        "inSR":          "4326",
        "spatialRel":    "esriSpatialRelIntersects",
        "returnCountOnly": "true",
        "f":             "json",
    }
    try:
        r3 = requests.get(
            f"{base}/FloodMapForPlanningRiversSeasFloodZone3/MapServer/0/query",
            params=params, timeout=8)
        if r3.status_code == 200 and r3.json().get("count", 0) > 0:
            return "Zone 3 (High)"
        r2 = requests.get(
            f"{base}/FloodMapForPlanningRiversSeasFloodZone2/MapServer/0/query",
            params=params, timeout=8)
        if r2.status_code == 200 and r2.json().get("count", 0) > 0:
            return "Zone 2 (Medium)"
        return "Zone 1 (Low)"
    except Exception:
        return "Unknown"

def run_park_intelligence(park, ch_api_key, epc_bearer_token):
    """
    Run all live API calls for a single park.
    Returns a dict: { ofcom, companies, epc, flood_risk, coords }
    Ofcom is still fetched from the local area_data.json.
    """
    postcode = park.get("postcode", "")
    la       = park.get("local_authority", "")
    ofcom    = get_ofcom(la)
    companies = get_companies(postcode, ch_api_key) if ch_api_key else []
    epc       = get_epc_data(postcode, epc_bearer_token)
    lat, lon  = get_postcode_coords(postcode)
    flood     = get_flood_risk(lat, lon)
    return {
        "ofcom":      ofcom,
        "companies":  companies,
        "epc":        epc,
        "flood_risk": flood,
        "coords":     {"lat": lat, "lon": lon},
    }

def score_connectivity(ofcom):
    if not ofcom:
        return None, "No data"
    ff = ofcom.get("full_fibre_pct", 0) or 0
    gig = ofcom.get("gigabit_pct", 0) or 0
    sup = ofcom.get("superfast_pct", 0) or 0
    no_decent = ofcom.get("no_decent_pct", 0) or 0
    score = min(40, ff * 0.4) + min(20, gig * 0.3) + min(20, sup * 0.2) + max(0, 20 - no_decent * 2)
    score = round(score)
    if score >= 70:
        rag = "Green"
    elif score >= 40:
        rag = "Amber"
    else:
        rag = "Red"
    return score, rag

def score_mobile(ofcom):
    if not ofcom:
        return None
    g4 = ofcom.get("indoor_4g_pct", 0) or 0
    g5 = ofcom.get("outdoor_5g_pct", 0) or 0
    voice = ofcom.get("indoor_voice_pct", 0) or 0
    score = min(40, g4 * 0.4) + min(40, g5 * 0.4) + min(20, voice * 0.2)
    return round(score)

def classify_companies(companies):
    sector_map = {
        "Retail": list(range(4710, 4800)),
        "Food & Beverage": list(range(5610, 5640)),
        "Finance & Insurance": list(range(6400, 6700)),
        "Professional Services": list(range(6900, 7600)),
        "Technology & IT": list(range(5800, 6400)),
        "Property & Real Estate": list(range(6800, 6900)),
        "Leisure & Entertainment": list(range(9000, 9330)),
        "Health & Beauty": [4775, 8600, 8610, 8620],
    }
    counts = {k: 0 for k in sector_map}
    for co in companies:
        if co.get("company_status", "").lower() != "active":
            continue
        for sic in co.get("sic_codes", []) or []:
            try:
                code = int(str(sic)[:4])
                for sector, codes in sector_map.items():
                    if code in codes:
                        counts[sector] += 1
            except Exception:
                pass
    return {k: v for k, v in counts.items() if v > 0}

def generate_opportunities(park, ofcom, companies, ws_data=None):
    """Generate retail-specific commercial opportunities."""
    ops = []
    ff    = ofcom.get("full_fibre_pct", 0) or 0
    gig   = ofcom.get("gigabit_pct", 0) or 0
    takeup= ofcom.get("full_fibre_takeup_pct", 0) or 0
    g4    = ofcom.get("indoor_4g_pct", 0) or 0
    g5    = ofcom.get("outdoor_5g_pct", 0) or 0

    asset_type  = (park.get("type") or "").lower()
    notes       = (park.get("notes") or "").lower()
    anchors     = [a.lower() for a in (park.get("anchor_tenants") or [])]
    gla         = park.get("gla_sqft", 0) or 0
    repositioning = park.get("repositioning", False)

    # Connectivity gaps
    if gig < 50:
        ops.append("Gigabit connectivity upgrade — current LA area below threshold for modern retail operations and guest WiFi")
    if ff < 60:
        ops.append("Full fibre estate upgrade — legacy broadband limiting retailer EPOS, security systems, and cloud point-of-sale")
    if ff > 60 and takeup < 30:
        ops.append("Managed connectivity migration — full fibre available but take-up low; active programme needed across retailer units")
    if g4 < 80:
        ops.append("Indoor mobile coverage enhancement — below threshold for customer experience, contactless payments, and staff communications")
    if g5 < 40:
        ops.append("5G readiness — outdoor coverage insufficient for smart centre applications, delivery management, and future IoT")

    # Scale-based
    if gla >= 1000000:
        ops.append(f"Estate-wide managed network — {gla:,} sq ft asset requires enterprise-grade multi-tenant infrastructure with centralised management")
    elif gla >= 500000:
        ops.append(f"Multi-tenant managed connectivity — {gla:,} sq ft with multiple anchor tenants benefits from single managed services provider")

    # Asset type specific
    if "regional" in asset_type or "sub-regional" in asset_type or gla >= 300000:
        # Check WiredScore status from ws_data (user input) or fall back to park data
        if ws_data:
            ws_status = (ws_data.get("wiredScore") or {}).get("status","unconfirmed")
            ss_status = (ws_data.get("smartScore") or {}).get("status","unconfirmed")
        else:
            ws_status = "unconfirmed"
            ss_status = "unconfirmed"
        if ws_status not in ("certified",):
            ops.append("WiredScore certification — major retail assets increasingly expected to hold certification; Modern Networks are Accredited Professionals")
        if ss_status not in ("certified",):
            ops.append("SmartScore certification — smart building accreditation differentiates repositioning assets for premium occupiers")
    if "outlet" in asset_type:
        ops.append("Premium brand connectivity — outlet centres require reliable high-bandwidth connections for brand experience, analytics, and stock management")
    if "retail park" in asset_type:
        ops.append("Click-and-collect infrastructure — retail parks are primary click-and-collect destinations requiring robust connectivity for real-time stock visibility")

    # Repositioning
    if repositioning:
        ops.append("Repositioning connectivity brief — redevelopment creates opportunity to specify modern network infrastructure from the outset rather than retrofit")

    # Anchor tenant triggers
    if any(a in anchors for a in ["vue cinema", "cineworld", "odeon", "showcase cinema"]):
        ops.append("Cinema-grade connectivity — high-bandwidth requirements for digital projection, online ticketing, and loyalty systems")
    if any(a in anchors for a in ["ikea"]):
        ops.append("Large format retail connectivity — IKEA and similar require dedicated high-capacity links for inventory management and customer systems")
    if any(x in notes for x in ["food court", "food hall", "restaurants", "dining", "f&b", "leisure"]):
        ops.append("F&B and leisure managed connectivity — food and leisure operators require reliable connectivity for reservation systems, EPOS, and guest WiFi")
    if any(x in notes for x in ["hotel", "w hotel", "marriott", "hilton"]):
        ops.append("Hotel-grade connectivity — mixed-use schemes with hotel components require segregated high-performance guest and operational networks")

    # Companies House density
    active_count = sum(1 for c in (companies or []) if c.get("company_status","").lower() == "active")
    if active_count >= 20:
        ops.append(f"Occupier base — {active_count}+ active registered companies at postcode; estate-wide managed connectivity covers all retailers in a single contract")

    return ops[:8] if len(ops) > 8 else ops


def generate_flags(park, ofcom):
    """Generate retail-specific intelligence flags."""
    flags = []
    ff    = ofcom.get("full_fibre_pct", 0) or 0
    gig   = ofcom.get("gigabit_pct", 0) or 0
    takeup= ofcom.get("full_fibre_takeup_pct", 0) or 0
    g4    = ofcom.get("indoor_4g_pct", 0) or 0
    g5    = ofcom.get("outdoor_5g_pct", 0) or 0

    asset_type    = (park.get("type") or "").lower()
    notes         = (park.get("notes") or "").lower()
    repositioning = park.get("repositioning", False)
    gla           = park.get("gla_sqft", 0) or 0

    if gig < 50:
        flags.append(("⚠ Gigabit coverage below threshold",
                      f"{gig:.1f}% — inadequate for modern multi-tenant retail operations"))
    if ff < 50:
        flags.append(("⚠ Full fibre below 50%",
                      f"{ff:.1f}% — legacy broadband infrastructure limits retailer digital capabilities"))
    elif ff < 75:
        flags.append(("⚠ Full fibre availability gap",
                      f"{ff:.1f}% — below 75% recommended for major retail schemes"))
    if ff > 50 and takeup < 25:
        flags.append(("⚠ Low take-up despite availability",
                      f"Full fibre available ({ff:.1f}%) but take-up only {takeup:.1f}% — managed migration programme needed"))
    if g4 < 75:
        flags.append(("⚠ Indoor 4G below threshold",
                      f"{g4:.1f}% across all operators — impacts customer experience, contactless payments, and staff communications"))
    if g5 < 30:
        flags.append(("⚠ 5G readiness low",
                      f"Outdoor 5G at {g5:.1f}% — limits smart centre and delivery management applications"))
    if repositioning:
        flags.append(("ℹ Repositioning / redevelopment",
                      "Asset undergoing or planning significant change — infrastructure brief is live opportunity"))
    if gla >= 1000000:
        flags.append(("ℹ Major regional asset",
                      f"{gla:,} sq ft GLA — requires enterprise-grade network with 99.9%+ uptime SLA"))
    if any(x in notes for x in ["vacant", "closure", "administration", "empty"]):
        flags.append(("⚠ Anchor vacancy",
                      "Potential anchor tenant closure — repositioning likely with new connectivity requirements"))

    return flags


NAVY   = colors.HexColor("#1F4E79")
TEAL   = colors.HexColor("#2E74B5")
LGREY  = colors.HexColor("#F2F4F7")
MGREY  = colors.HexColor("#D0D9E8")
AMBER  = colors.HexColor("#C55A00")
GREEN  = colors.HexColor("#375623")
RED_C  = colors.HexColor("#C00000")
WHITE  = colors.white

RAG_COLORS = {"Green": GREEN, "Amber": AMBER, "Red": RED_C}

def get_styles():
    styles = getSampleStyleSheet()
    return {
        "title": ParagraphStyle("title", fontSize=22, textColor=WHITE, fontName="Helvetica-Bold", spaceAfter=4),
        "subtitle": ParagraphStyle("subtitle", fontSize=11, textColor=colors.HexColor("#BDD7EE"), fontName="Helvetica"),
        "h2": ParagraphStyle("h2", fontSize=13, textColor=NAVY, fontName="Helvetica-Bold", spaceBefore=12, spaceAfter=4),
        "h3": ParagraphStyle("h3", fontSize=11, textColor=TEAL, fontName="Helvetica-Bold", spaceBefore=8, spaceAfter=3),
        "body": ParagraphStyle("body", fontSize=9, textColor=colors.HexColor("#2C2C2C"), fontName="Helvetica", spaceAfter=4, leading=13),
        "small": ParagraphStyle("small", fontSize=7.5, textColor=colors.HexColor("#595959"), fontName="Helvetica-Oblique"),
        "caveat": ParagraphStyle("caveat", fontSize=8, textColor=colors.HexColor("#595959"), fontName="Helvetica-Oblique", spaceBefore=4),
        "flag": ParagraphStyle("flag", fontSize=9, textColor=AMBER, fontName="Helvetica-Bold"),
        "flagbody": ParagraphStyle("flagbody", fontSize=8.5, textColor=colors.HexColor("#2C2C2C"), fontName="Helvetica"),
        "opp": ParagraphStyle("opp", fontSize=9, textColor=NAVY, fontName="Helvetica"),
    }

def header_row(cells, widths, bg=None):
    row = [[Paragraph(str(c), ParagraphStyle("th", fontSize=8.5, textColor=WHITE, fontName="Helvetica-Bold"))] for c in cells]
    t = Table([row], colWidths=widths)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg or NAVY),
        ("PADDING", (0, 0), (-1, -1), 5),
        ("GRID", (0, 0), (-1, -1), 0.5, MGREY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t

def data_table(headers, rows, col_widths, zebra=True):
    h_style = ParagraphStyle("th2", fontSize=8, textColor=WHITE, fontName="Helvetica-Bold")
    b_style = ParagraphStyle("td", fontSize=8.5, textColor=colors.HexColor("#2C2C2C"), fontName="Helvetica")
    data = [[Paragraph(str(h), h_style) for h in headers]]
    for r in rows:
        data.append([Paragraph(str(c or ""), b_style) for c in r])
    t = Table(data, colWidths=col_widths, repeatRows=1)
    ts = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("GRID", (0, 0), (-1, -1), 0.4, MGREY),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("PADDING", (0, 0), (-1, -1), 5),
    ]
    if zebra:
        for i in range(1, len(data)):
            if i % 2 == 0:
                ts.append(("BACKGROUND", (0, i), (-1, i), LGREY))
    t.setStyle(TableStyle(ts))
    return t

def score_bar_table(label, score, rag, width=170*mm):
    bar_w = int((score or 0) / 100 * (width - 90*mm))
    bar_cell = Table([[""]], colWidths=[bar_w], rowHeights=[8*mm])
    bar_cell.setStyle(TableStyle([("BACKGROUND", (0, 0), (0, 0), RAG_COLORS.get(rag, MGREY))]))
    score_str = f"{score}/100  [{rag}]" if score is not None else "No data"
    row = [
        [Paragraph(label, ParagraphStyle("sl", fontSize=9, fontName="Helvetica-Bold", textColor=NAVY))],
        [bar_cell],
        [Paragraph(score_str, ParagraphStyle("sv", fontSize=9, fontName="Helvetica-Bold",
                                              textColor=RAG_COLORS.get(rag, MGREY)))],
    ]
    t = Table([row], colWidths=[50*mm, width - 90*mm, 40*mm])
    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0,0),(-1,-1), 0)]))
    return t

def build_park_header(story, park, styles):
    region = park.get("_region", "")
    cluster = park.get("_cluster", "")
    header_data = [[
        Paragraph(park["name"], styles["title"]),
        Paragraph(f"{park.get('location','')} · {park.get('postcode','')}", styles["subtitle"]),
        Paragraph(f"{region}  ›  {cluster}", styles["subtitle"]),
    ]]
    t = Table([header_data], colWidths=[180*mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("PADDING", (0, 0), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(t)
    story.append(Spacer(1, 6*mm))

def build_park_profile_table(story, park, styles, ws_data=None):
    story.append(Paragraph("Asset Profile", styles["h2"]))
    fields = [
        ("Cluster / Area",    park.get("_cluster", "")),
        ("Asset Type",        park.get("type", "")),
        ("Postcode",          park.get("postcode", "")),
        ("Local Authority",   park.get("local_authority", "")),
        ("GLA",               f"{park.get('gla_sqft',0):,} sq ft" if park.get('gla_sqft') else "—"),
        ("Landlord",          park.get("landlord", "")),
        ("Anchor Tenants",    ", ".join(park.get("anchor_tenants") or [])),
        ("Repositioning",     "Yes — active redevelopment" if park.get("repositioning") else "No"),
        ("WiredScore",        _ws_label(ws_data, "wiredScore") if ws_data else park.get("wiredScore") or "Not verified — check wiredscore.com/map"),
        ("SmartScore",        _ws_label(ws_data, "smartScore") if ws_data else park.get("smartScore") or "Not verified — check wiredscore.com/map"),
        ("Status",            park.get("status", "")),
        ("Website",           park.get("website", "")),
    ]
    body_s = ParagraphStyle("td2", fontSize=8.5, fontName="Helvetica", textColor=colors.HexColor("#2C2C2C"))
    key_s = ParagraphStyle("tk", fontSize=8.5, fontName="Helvetica-Bold", textColor=NAVY)
    rows = []
    for i, (k, v) in enumerate(fields):
        bg = LGREY if i % 2 == 0 else WHITE
        rows.append(Table([[Paragraph(k, key_s), Paragraph(str(v), body_s)]],
                          colWidths=[50*mm, 120*mm]))
    t = Table([[r] for r in rows], colWidths=[170*mm])
    t.setStyle(TableStyle([("BACKGROUND", (0, i), (-1, i), LGREY if i % 2 == 0 else WHITE) for i in range(len(rows))] +
                           [("GRID", (0, 0), (-1, -1), 0.4, MGREY), ("PADDING", (0, 0), (-1, -1), 0)]))
    story.append(t)
    if park.get("notes"):
        story.append(Spacer(1, 3*mm))
        story.append(Paragraph(f"<b>Notes:</b> {park['notes']}", styles["body"]))
    story.append(Spacer(1, 5*mm))

def build_connectivity_section(story, ofcom, styles):
    story.append(Paragraph("Connectivity Profile", styles["h2"]))
    conn_score, conn_rag = score_connectivity(ofcom)
    mob_score = score_mobile(ofcom)

    if not ofcom:
        story.append(Paragraph("Ofcom data not available for this local authority.", styles["body"]))
        return

    story.append(score_bar_table("Broadband Connectivity Score", conn_score, conn_rag))
    story.append(Spacer(1, 3*mm))

    conn_rows = [
        ["Full Fibre availability", f"{ofcom.get('full_fibre_pct', 'N/A'):.1f}%" if ofcom.get('full_fibre_pct') is not None else 'N/A', "% of premises with full fibre available"],
        ["Gigabit-capable", f"{ofcom.get('gigabit_pct', 'N/A'):.1f}%" if ofcom.get('gigabit_pct') is not None else 'N/A', "% of premises with gigabit broadband"],
        ["Superfast (30Mbps+)", f"{ofcom.get('superfast_pct', 'N/A'):.1f}%" if ofcom.get('superfast_pct') is not None else 'N/A', "% of premises with superfast coverage"],
        ["No decent broadband", f"{ofcom.get('no_decent_pct', 'N/A'):.1f}%" if ofcom.get('no_decent_pct') is not None else 'N/A', "% with speeds below 10Mbps / 1Mbps"],
        ["Full fibre take-up", f"{ofcom.get('full_fibre_takeup_pct', 'N/A'):.1f}%" if ofcom.get('full_fibre_takeup_pct') is not None else 'N/A', "% of businesses on full fibre connections"],
        ["Avg monthly data usage", f"{ofcom.get('avg_data_usage_gb', 'N/A')} GB" if ofcom.get('avg_data_usage_gb') is not None else 'N/A', "Average monthly usage per connection"],
    ]
    story.append(data_table(["Metric", "Value", "Notes"], conn_rows, [65*mm, 35*mm, 70*mm]))
    story.append(Paragraph("Data: Ofcom Connected Nations, Jul 2024. Local authority level — asset-specific connectivity may differ. On-site survey recommended.", styles["caveat"]))
    story.append(Spacer(1, 5*mm))

    story.append(Paragraph("Mobile Coverage", styles["h3"]))
    if mob_score is not None:
        story.append(score_bar_table("Mobile Coverage Score", mob_score, "Green" if mob_score >= 70 else "Amber" if mob_score >= 40 else "Red", width=170*mm))
    story.append(Spacer(1, 3*mm))
    mob_rows = [
        ["Indoor 4G (all operators)", f"{ofcom.get('indoor_4g_pct', 'N/A'):.1f}%" if ofcom.get('indoor_4g_pct') is not None else 'N/A'],
        ["Outdoor 4G (all operators)", f"{ofcom.get('outdoor_4g_pct', 'N/A'):.1f}%" if ofcom.get('outdoor_4g_pct') is not None else 'N/A'],
        ["Outdoor 5G (all operators)", f"{ofcom.get('outdoor_5g_pct', 'N/A'):.1f}%" if ofcom.get('outdoor_5g_pct') is not None else 'N/A'],
        ["Indoor voice (all operators)", f"{ofcom.get('indoor_voice_pct', 'N/A'):.1f}%" if ofcom.get('indoor_voice_pct') is not None else 'N/A'],
    ]
    story.append(data_table(["Coverage Metric", "Coverage %"], mob_rows, [100*mm, 70*mm]))
    story.append(Spacer(1, 5*mm))

def build_companies_section(story, companies, park, styles):
    story.append(Paragraph(f"Registered Companies at Postcode ({park.get('postcode','')})", styles["h2"]))
    if not companies:
        story.append(Paragraph("Companies House data not available (API key not configured or no results found).", styles["body"]))
        story.append(Spacer(1, 5*mm))
        return
    active = [c for c in companies if c.get("company_status", "").lower() == "active"]
    sectors = classify_companies(companies)
    story.append(Paragraph(f"<b>Results found:</b> {len(companies)} companies · <b>Active:</b> {len(active)} · <b>Sector profile:</b> {', '.join(f'{k} ({v})' for k, v in sectors.items()) or 'Mixed'}", styles["body"]))
    story.append(Spacer(1, 3*mm))
    rows = []
    for c in companies[:15]:
        rows.append([
            c.get("title", ""),
            c.get("company_status", "").capitalize(),
            c.get("date_of_creation", "")[:4] if c.get("date_of_creation") else "",
            ", ".join((c.get("sic_codes") or [])[:2]),
        ])
    if rows:
        story.append(data_table(["Company Name", "Status", "Inc.", "SIC Codes"], rows, [75*mm, 25*mm, 20*mm, 50*mm]))
    story.append(Spacer(1, 5*mm))

def build_intelligence_section(story, flags, opportunities, styles):
    story.append(PageBreak())
    story.append(Paragraph("Intelligence Flags", styles["h2"]))
    if not flags:
        story.append(Paragraph("No significant intelligence flags identified for this retail asset.", styles["body"]))
    for flag_title, flag_detail in flags:
        story.append(Paragraph(flag_title, styles["flag"]))
        story.append(Paragraph(flag_detail, styles["flagbody"]))
        story.append(Spacer(1, 3*mm))

    story.append(Spacer(1, 5*mm))
    story.append(Paragraph("Commercial Opportunities", styles["h2"]))
    if not opportunities:
        story.append(Paragraph("No specific opportunities identified. Consider on-site survey for detailed assessment.", styles["body"]))
    for i, opp in enumerate(opportunities, 1):
        story.append(Paragraph(f"{i}.  {opp}", styles["opp"]))
        story.append(Spacer(1, 2*mm))

    story.append(Spacer(1, 8*mm))
    story.append(Paragraph("Recommended Next Steps", styles["h2"]))
    steps = [
        ["1", "On-site connectivity audit", "Commission independent survey to validate LA-level Ofcom data with asset-specific measurements for each retail unit"],
        ["2", "Landlord / asset manager briefing", "Present findings to the landlord or asset manager as a value-led conversation opener — not a sales pitch"],
        ["3", "Anchor tenant mapping", "Map individual anchor tenant connectivity requirements — particularly cinema, F&B, and fashion retailers with high bandwidth needs"],
        ["4", "WiredScore / SmartScore assessment", "Assess asset against WiredScore and SmartScore certification criteria — Modern Networks are Accredited Professionals"],
    ]
    story.append(data_table(["", "Action", "Description"], steps, [10*mm, 50*mm, 110*mm]))

def build_epc_flood_section(story, epc, flood_risk, styles):
    story.append(Paragraph("Energy Performance & Flood Risk", styles["h2"]))
    epc_rag   = "Green" if (epc or {}).get("most_common","") in ("A","B","C") else                 "Amber" if (epc or {}).get("most_common","") == "D" else                 "Red"   if (epc or {}).get("most_common","") in ("E","F","G") else None
    flood_rag = {"Zone 1 (Low)":"Green","Zone 2 (Medium)":"Amber","Zone 3 (High)":"Red"}.get(flood_risk or "","")

    rows = []
    if epc:
        mc  = epc.get("most_common","—")
        abc = epc.get("abc_pct", 0)
        tot = epc.get("total", 0)
        ratings_str = "  ".join(f"{k}:{v}" for k,v in sorted((epc.get("ratings") or {}).items()))
        rows += [
            ["EPC most common rating", mc, "Based on non-domestic certificates at this postcode"],
            ["EPC A–C rated",          f"{abc}%", f"{tot} certificates found"],
            ["EPC breakdown",          ratings_str[:60], "All ratings found at postcode"],
        ]
        if mc not in ("A","B","C") and mc != "—":
            rows.append(["⚠ Below 2027 minimum",
                         "Likely below proposed EPC C threshold",
                         "Energy upgrade conversation recommended"])
    else:
        rows.append(["EPC data", "Not available", "No non-domestic certificates found for this postcode"])

    flood_label = flood_risk or "Unknown"
    flood_note  = {"Zone 3 (High)":  "High probability — resilience and continuity planning recommended",
                   "Zone 2 (Medium)":"Medium probability — consider in site resilience assessment",
                   "Zone 1 (Low)":   "Low flood risk — no specific constraints identified",
                   "Unknown":        "Could not be determined — check Environment Agency mapping"}.get(flood_label, "")
    rows.append(["Flood risk (EA)", flood_label, flood_note])

    story.append(data_table(["Metric", "Value", "Notes"], rows, [45*mm, 40*mm, 85*mm]))
    story.append(Spacer(1, 5*mm))

def _ws_label(ws_data, key):
    """Format WiredScore or SmartScore entry for display."""
    if not ws_data:
        return "Not verified — check wiredscore.com/map"
    d = ws_data.get(key, {})
    status = d.get("status", "unconfirmed")
    if status == "certified":
        scheme = d.get("scheme", "")
        level  = d.get("level", "")
        return f"✓ Certified — {scheme} {level}".strip()
    elif status == "not-certified":
        return "✕ Not certified"
    return "Not verified — check wiredscore.com/map"

def generate_park_pdf(park, ofcom, companies, epc=None, flood_risk=None, ws_data=None):
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                             leftMargin=15*mm, rightMargin=15*mm,
                             topMargin=15*mm, bottomMargin=20*mm,
                             title=f"Intelligence Report: {park['name']}")
    styles = get_styles()
    story = []

    build_park_header(story, park, styles)
    build_park_profile_table(story, park, styles, ws_data=ws_data)
    story.append(HRFlowable(width="100%", thickness=1, color=MGREY, spaceAfter=4*mm))
    build_connectivity_section(story, ofcom, styles)
    story.append(HRFlowable(width="100%", thickness=1, color=MGREY, spaceAfter=4*mm))
    build_companies_section(story, companies, park, styles)
    story.append(HRFlowable(width="100%", thickness=1, color=MGREY, spaceAfter=4*mm))
    build_epc_flood_section(story, epc, flood_risk, styles)

    flags = generate_flags(park, ofcom) if ofcom else []
    ops = generate_opportunities(park, ofcom or {}, companies or [])
    build_intelligence_section(story, flags, ops, styles)

    story.append(Spacer(1, 10*mm))
    story.append(Paragraph(
        f"Generated: {datetime.datetime.now().strftime('%d %B %Y %H:%M')} · Data: Ofcom Connected Nations Jul 2024 · EPC Register · Companies House · Environment Agency · INTERNAL USE ONLY — Modern Networks Retail Intelligence",
        styles["small"]
    ))

    doc.build(story)
    buf.seek(0)
    return buf

# ─── PDF GENERATION: AREA / CLUSTER / REGION REPORT ──────────────────────────
def generate_area_pdf(area_label, parks_list, all_ofcom_results, report_title, all_intelligence=None, area_ws=None):
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                             leftMargin=15*mm, rightMargin=15*mm,
                             topMargin=15*mm, bottomMargin=20*mm,
                             title=report_title)
    styles = get_styles()
    story = []

    header_data = [[
        Paragraph(report_title, styles["title"]),
        Paragraph(f"{len(parks_list)} assets profiled  ·  {area_label}", styles["subtitle"]),
        Paragraph(f"Generated {datetime.datetime.now().strftime('%d %B %Y')}", styles["subtitle"]),
    ]]
    t = Table([header_data], colWidths=[180*mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("PADDING", (0, 0), (-1, -1), 10),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(t)
    story.append(Spacer(1, 6*mm))

    story.append(Paragraph("Area Summary", styles["h2"]))
    scored = [(p, all_ofcom_results.get(p["id"]), score_connectivity(all_ofcom_results.get(p["id"]) or {})[0])
              for p in parks_list]
    with_scores = [(p, o, s) for p, o, s in scored if s is not None]
    if with_scores:
        avg_score = round(sum(s for _, _, s in with_scores) / len(with_scores))
        green = sum(1 for _, _, s in with_scores if s >= 70)
        amber = sum(1 for _, _, s in with_scores if 40 <= s < 70)
        red   = sum(1 for _, _, s in with_scores if s < 40)
        intel_run = all_intelligence is not None
        epc_count  = sum(1 for p in parks_list
                         if (all_intelligence or {}).get(p["id"], {}).get("epc")) if intel_run else 0
        flood_high = sum(1 for p in parks_list
                         if (all_intelligence or {}).get(p["id"], {}).get("flood_risk","") == "Zone 3 (High)") if intel_run else 0
        summary_rows = [
            ["Assets in area", str(len(parks_list)), "Average connectivity score", f"{avg_score}/100"],
            ["Green RAG", str(green), "Amber RAG", str(amber)],
            ["Red RAG", str(red), "Assets with Ofcom data", str(len(with_scores))],
        ]
        if intel_run:
            summary_rows += [
                ["Assets with EPC data", str(epc_count), "High flood risk assets", str(flood_high)],
            ]
        body_s = ParagraphStyle("sb", fontSize=9, fontName="Helvetica", textColor=colors.HexColor("#2C2C2C"))
        key_s = ParagraphStyle("sk", fontSize=9, fontName="Helvetica-Bold", textColor=NAVY)
        tbl = Table(
            [[Paragraph(r[0], key_s), Paragraph(r[1], body_s), Paragraph(r[2], key_s), Paragraph(r[3], body_s)] for r in summary_rows],
            colWidths=[55*mm, 30*mm, 55*mm, 30*mm]
        )
        tbl.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, MGREY),
            ("BACKGROUND", (0, 0), (0, -1), LGREY), ("BACKGROUND", (2, 0), (2, -1), LGREY),
            ("PADDING", (0, 0), (-1, -1), 6),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(tbl)
    story.append(Spacer(1, 6*mm))

    story.append(Paragraph("Connectivity Comparison — All Assets (Ranked)", styles["h2"]))
    ranked = sorted(scored, key=lambda x: (x[2] is None, -(x[2] or 0)))

    comp_rows = []
    for rank, (park, ofcom, conn_score) in enumerate(ranked, 1):
        rag       = score_connectivity(ofcom or {})[1] if ofcom else "No data"
        ff        = f"{ofcom.get('full_fibre_pct', 0):.0f}%" if ofcom else "—"
        gig       = f"{ofcom.get('gigabit_pct', 0):.0f}%" if ofcom else "—"
        g4        = f"{ofcom.get('indoor_4g_pct', 0):.0f}%" if ofcom else "—"
        g5        = f"{ofcom.get('outdoor_5g_pct', 0):.0f}%" if ofcom else "—"
        score_str = f"{conn_score}/100" if conn_score is not None else "—"
        intel     = (all_intelligence or {}).get(park["id"], {})
        epc_str   = intel.get("epc", {}).get("most_common", "—") if intel else "—"
        flood_str = intel.get("flood_risk", "—") if intel else "—"
        flood_short = {"Zone 3 (High)":"Z3","Zone 2 (Medium)":"Z2","Zone 1 (Low)":"Z1"}.get(flood_str, "—")
        if all_intelligence:
            comp_rows.append([str(rank), park["name"][:25], park.get("local_authority","")[:16],
                              score_str, rag, ff, gig, g4, g5, epc_str, flood_short])
        else:
            comp_rows.append([str(rank), park["name"][:28], park.get("local_authority","")[:18],
                              score_str, rag, ff, gig, g4, g5])

    if all_intelligence:
        story.append(data_table(
            ["#", "Asset", "Local Authority", "Score", "RAG", "FF%", "Gig%", "4G%", "5G%", "EPC", "Flood"],
            comp_rows,
            [7*mm, 40*mm, 28*mm, 15*mm, 14*mm, 11*mm, 11*mm, 11*mm, 11*mm, 11*mm, 11*mm]
        ))
    else:
        story.append(data_table(
            ["#", "Asset", "Local Authority", "Score", "RAG", "FF%", "Gig%", "4G%", "5G%"],
            comp_rows,
            [8*mm, 48*mm, 32*mm, 17*mm, 16*mm, 13*mm, 13*mm, 12*mm, 12*mm]
        ))
    story.append(Paragraph("Data: Ofcom Connected Nations Jul 2024 · EPC Register · Environment Agency · Local authority level — asset-specific connectivity may differ.", styles["caveat"]))
    story.append(Spacer(1, 6*mm))

    all_ops = {}
    for park in parks_list:
        ofcom    = all_ofcom_results.get(park["id"]) or {}
        pid      = park.get("id","")
        pws      = (area_ws or {}).get(pid, {})
        park_ops = generate_opportunities(park, ofcom, [], ws_data=pws if pws else None)
        for op in park_ops:
            all_ops[op] = all_ops.get(op, 0) + 1

    if all_ops:
        story.append(Paragraph("Most Common Opportunities Across Area", styles["h2"]))
        sorted_ops = sorted(all_ops.items(), key=lambda x: -x[1])
        op_rows = [[str(v), op] for op, v in sorted_ops[:10]]
        story.append(data_table(["Assets", "Opportunity"], op_rows, [20*mm, 150*mm]))
        story.append(Spacer(1, 6*mm))

    story.append(PageBreak())
    story.append(Paragraph("Individual Asset Summaries", styles["h2"]))

    for park in parks_list:
        ofcom    = all_ofcom_results.get(park["id"]) or {}
        pid_a    = park.get("id","")
        pws_a    = (area_ws or {}).get(pid_a, {})
        conn_score, conn_rag = score_connectivity(ofcom)
        mob_score = score_mobile(ofcom)
        flags = generate_flags(park, ofcom) if ofcom else []
        ops   = generate_opportunities(park, ofcom, [], ws_data=pws_a if pws_a else None)

        ps = ParagraphStyle("pname", fontSize=11, fontName="Helvetica-Bold", textColor=WHITE)
        ps2 = ParagraphStyle("ploc", fontSize=9, fontName="Helvetica", textColor=colors.HexColor("#BDD7EE"))
        park_hdr = Table([[Paragraph(park["name"], ps)], [Paragraph(f"{park.get('type','')} · {park.get('landlord','')[:50]}", ps2)]],
                         colWidths=[170*mm])
        park_hdr.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), TEAL), ("PADDING", (0, 0), (-1, -1), 7)]))
        story.append(KeepTogether([
            park_hdr,
            Spacer(1, 2*mm),
        ]))

        cs_str  = f"{conn_score}/100 [{conn_rag}]" if conn_score is not None else "No Ofcom data"
        ms_str  = f"{mob_score}/100" if mob_score is not None else "—"
        p_intel = (all_intelligence or {}).get(park["id"], {})
        epc_str = p_intel.get("epc", {}).get("most_common", "—") if p_intel else "—"
        flood_s = p_intel.get("flood_risk", "—") if p_intel else "—"
        gla_str = f"{park.get('gla_sqft',0):,} sq ft" if park.get("gla_sqft") else "—"
        pid     = park.get("id","")
        pws     = (area_ws or {}).get(pid, {})
        ws_disp = _ws_label(pws, "wiredScore") if pws else "Not verified"
        ss_disp = _ws_label(pws, "smartScore") if pws else "Not verified"
        mini_rows = [
            ["Connectivity Score", cs_str, "Mobile Score", ms_str],
            ["Full Fibre %", f"{ofcom.get('full_fibre_pct',0):.0f}%" if ofcom else "—",
             "5G Outdoor %", f"{ofcom.get('outdoor_5g_pct',0):.0f}%" if ofcom else "—"],
            ["EPC Rating", epc_str, "Flood Risk", flood_s],
            ["WiredScore", ws_disp[:35], "SmartScore", ss_disp[:35]],
            ["Asset Type", park.get("type","")[:40], "GLA", gla_str],
            ["Landlord", park.get("landlord","")[:40], "Status", park.get("status","")],
        ]
        key_s2 = ParagraphStyle("mk", fontSize=8, fontName="Helvetica-Bold", textColor=NAVY)
        val_s2 = ParagraphStyle("mv", fontSize=8, fontName="Helvetica", textColor=colors.HexColor("#2C2C2C"))
        mini_t = Table(
            [[Paragraph(r[0], key_s2), Paragraph(str(r[1]), val_s2), Paragraph(r[2], key_s2), Paragraph(str(r[3]), val_s2)] for r in mini_rows],
            colWidths=[40*mm, 45*mm, 40*mm, 45*mm]
        )
        mini_t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, MGREY),
            ("BACKGROUND", (0, 0), (0, -1), LGREY), ("BACKGROUND", (2, 0), (2, -1), LGREY),
            ("PADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(mini_t)

        if flags:
            flag_text = "  ·  ".join(f[0] for f in flags[:3])
            story.append(Paragraph(f"Flags: {flag_text}", ParagraphStyle("fl", fontSize=8, fontName="Helvetica", textColor=AMBER, spaceBefore=3)))
        if ops:
            story.append(Paragraph(f"Top opportunity: {ops[0]}", ParagraphStyle("op1", fontSize=8, fontName="Helvetica", textColor=NAVY, spaceBefore=2)))
        story.append(Spacer(1, 5*mm))

    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width="100%", thickness=1, color=MGREY))
    story.append(Spacer(1, 3*mm))
    story.append(Paragraph(
        f"Generated: {datetime.datetime.now().strftime('%d %B %Y %H:%M')} · Data: Ofcom Connected Nations Jul 2024 · EPC Register · Environment Agency · INTERNAL USE ONLY",
        styles["small"]
    ))

    doc.build(story)
    buf.seek(0)
    return buf

# ─── MAIN UI ──────────────────────────────────────────────────────────────────
st.title("🏬 UK Retail Property Intelligence")
st.markdown("*National prospecting tool — digital infrastructure profiling for UK retail and leisure property*")

with st.sidebar:
    st.header("⚙️ Settings")
    ch_api_key = st.secrets.get("CH_API_KEY", "") if hasattr(st, "secrets") else ""
    if not ch_api_key:
        ch_api_key = st.text_input("Companies House API Key", type="password",
                                    help="Free key from developer.company-information.service.gov.uk")
    else:
        st.success("✓ Companies House API key loaded")

    epc_bearer_token = ""
    if hasattr(st, "secrets"):
        epc_bearer_token = st.secrets.get("api_keys", {}).get("epc_bearer_token", "")
    if epc_bearer_token:
        st.success("✓ EPC token loaded")
    else:
        st.warning("EPC token not set — add epc_bearer_token under [api_keys] in secrets")

    intelligence_available = bool(ch_api_key and epc_bearer_token)


    st.divider()
    st.divider()
    st.markdown("**About**")
    st.markdown(f"🏬 **{sum(len(c['assets']) for r in assets_data['regions'] for c in r['clusters'])} assets** indexed")
    st.markdown(f"🗺️ **{len(assets_data['regions'])} regions** · **{sum(len(r['clusters']) for r in assets_data['regions'])} clusters**")
    st.markdown("📡 Ofcom Connected Nations (Jul 2024)")
    st.markdown("🏛️ Companies House API (live)")

st.divider()

region_map = {r["name"]: r for r in assets_data["regions"]}

col1, col2, col3 = st.columns(3)

with col1:
    region_options = ["— Select a Region —"] + [r["name"] for r in assets_data["regions"]]
    selected_region_name = st.selectbox("1️⃣ Select Region", region_options)

if selected_region_name == "— Select a Region —":
    st.info("👆 Select a region to begin, then drill down to a cluster and individual asset — or generate an area-wide report.")
    st.stop()

selected_region = region_map[selected_region_name]

with col2:
    cluster_options = ["All clusters in this region"] + [c["name"] for c in selected_region["clusters"]]
    selected_cluster_name = st.selectbox("2️⃣ Select Cluster", cluster_options)

all_clusters_mode = selected_cluster_name == "All clusters in this region"

if not all_clusters_mode:
    selected_cluster = next(c for c in selected_region["clusters"] if c["name"] == selected_cluster_name)
    parks_in_scope = selected_cluster["assets"]
else:
    parks_in_scope = [p for c in selected_region["clusters"] for p in c["assets"]]

with col3:
    if all_clusters_mode:
        park_options = ["All assets in region"]
        park_label = f"All assets — {selected_region_name}"
    else:
        park_options = [f"All assets in {selected_cluster_name}"] + [p["name"] for p in parks_in_scope]
        park_label = None
    selected_park_name = st.selectbox("3️⃣ Select Asset", park_options)

all_parks_mode = selected_park_name.startswith("All assets")

if not all_parks_mode:
    selected_park = next(p for p in parks_in_scope if p["name"] == selected_park_name)
    region_name = selected_region_name
    cluster_name = selected_cluster_name if not all_clusters_mode else next(
        c["name"] for c in selected_region["clusters"] if any(p["id"] == selected_park["id"] for p in c["assets"])
    )
    selected_park["_region"] = region_name
    selected_park["_cluster"] = cluster_name

st.divider()

# ─── SINGLE PARK MODE ─────────────────────────────────────────────────────────
if not all_parks_mode:
    park = selected_park
    st.subheader(f"🏢 {park['name']}")
    subcols = st.columns(4)
    subcols[0].metric("Region", selected_region_name.split("&")[0].strip()[:20])
    subcols[1].metric("Cluster", cluster_name[:22])
    subcols[2].metric("Type", park.get("type","")[:22])
    subcols[3].metric("GLA", f"{park.get('gla_sqft',0):,} sq ft" if park.get("gla_sqft") else "—")

    with st.expander("🏅 WiredScore / SmartScore — optional, enter before generating report"):
        st.caption("Check status at wiredscore.com/map — leave as 'Not verified' if unknown.")
        ws_col1, ws_col2 = st.columns(2)
        with ws_col1:
            st.markdown("**WiredScore**")
            ws_status = st.selectbox("Status", 
                ["Not verified", "Certified", "Not certified"],
                key="ws_status")
            ws_scheme = ""
            ws_level  = ""
            if ws_status == "Certified":
                ws_scheme = st.selectbox("Scheme", ["WiredScore", "WiredScore for Retail Parks"], key="ws_scheme")
                ws_level  = st.selectbox("Level",  ["Certified", "Silver", "Gold", "Platinum"], key="ws_level")
        with ws_col2:
            st.markdown("**SmartScore**")
            ss_status = st.selectbox("Status ",
                ["Not verified", "Certified", "Not certified"],
                key="ss_status")
            ss_level  = ""
            if ss_status == "Certified":
                ss_level = st.selectbox("Level ", ["Certified", "Silver", "Gold", "Platinum"], key="ss_level")

    if st.button("🔍 Generate Asset Intelligence Report", type="primary", use_container_width=True):
        with st.spinner("Pulling data for this retail asset..."):
            ofcom     = get_ofcom(park.get("local_authority",""))
            companies = get_companies(park.get("postcode",""), ch_api_key) if ch_api_key else []
            epc       = get_epc_data(park.get("postcode",""), epc_bearer_token)
            lat, lon  = get_postcode_coords(park.get("postcode",""))
            flood     = get_flood_risk(lat, lon)
            conn_score, conn_rag = score_connectivity(ofcom)
            mob_score = score_mobile(ofcom)
            flags     = generate_flags(park, ofcom) if ofcom else []
            ws_data_single = {
                "wiredScore": {"status": ws_status.lower().replace(" ","-") if ws_status != "Not verified" else "unconfirmed",
                               "scheme": ws_scheme, "level": ws_level},
                "smartScore":  {"status": ss_status.lower().replace(" ","-") if ss_status != "Not verified" else "unconfirmed",
                               "level": ss_level},
            }
            ops       = generate_opportunities(park, ofcom or {}, companies, ws_data=ws_data_single)

        m1, m2, m3, m4 = st.columns(4)
        rag_icon = {"Green": "🟢", "Amber": "🟡", "Red": "🔴"}.get(conn_rag, "⚪")
        m1.metric("Connectivity Score", f"{conn_score}/100 {rag_icon}" if conn_score else "No data")
        m2.metric("Mobile Score", f"{mob_score}/100" if mob_score else "No data")
        m3.metric("Companies found", len(companies) if companies else "—")
        m4.metric("Opportunities", len(ops))

        st.divider()
        left, right = st.columns(2)

        with left:
            st.markdown("**📡 Connectivity Profile**")
            if ofcom:
                st.progress(min(1.0, (conn_score or 0)/100), text=f"Score: {conn_score}/100 [{conn_rag}]")
                conn_display = {
                    "Full Fibre %": f"{ofcom.get('full_fibre_pct',0):.1f}%",
                    "Gigabit %": f"{ofcom.get('gigabit_pct',0):.1f}%",
                    "Superfast %": f"{ofcom.get('superfast_pct',0):.1f}%",
                    "No decent BB": f"{ofcom.get('no_decent_pct',0):.1f}%",
                    "FF Take-up": f"{ofcom.get('full_fibre_takeup_pct',0):.1f}%",
                    "Avg data use": f"{ofcom.get('avg_data_usage_gb',0)} GB/mo",
                }
                for k, v in conn_display.items():
                    st.text(f"  {k}: {v}")
                st.markdown("**📱 Mobile**")
                mob_display = {
                    "Indoor 4G": f"{ofcom.get('indoor_4g_pct',0):.1f}%",
                    "Outdoor 4G": f"{ofcom.get('outdoor_4g_pct',0):.1f}%",
                    "Outdoor 5G": f"{ofcom.get('outdoor_5g_pct',0):.1f}%",
                    "Indoor voice": f"{ofcom.get('indoor_voice_pct',0):.1f}%",
                }
                for k, v in mob_display.items():
                    st.text(f"  {k}: {v}")
            else:
                st.warning("Ofcom data not available for this local authority.")

        with right:
            st.markdown("**🏬 Asset Profile**")
            st.text(f"  Type:     {park.get('type','—')[:45]}")
            st.text(f"  Landlord: {park.get('landlord','—')[:45]}")
            st.text(f"  GLA:      {park.get('gla_sqft',0):,} sq ft" if park.get("gla_sqft") else "  GLA:      —")
            st.text(f"  Status:   {park.get('status','—')}")
            st.text(f"  LA:       {park.get('local_authority','—')}")
            anchors = park.get("anchor_tenants") or []
            if anchors:
                st.text(f"  Anchors:  {', '.join(anchors[:3])}")
            if park.get("notes"):
                st.caption(park["notes"][:200])
            if companies:
                active = [c for c in companies if c.get("company_status","").lower()=="active"]
                st.markdown(f"**🏛️ Companies House** — {len(companies)} found, {len(active)} active")
                with st.expander(f"View companies ({min(15,len(companies))})"):
                    for c in companies[:15]:
                        st.text(f"• {c.get('title','')} [{c.get('company_status','').capitalize()}]")

            # ── EPC & Flood ───────────────────────────────────────────────────
            st.divider()
            epc_col, flood_col = st.columns(2)
            with epc_col:
                st.markdown("**⚡ EPC Ratings**")
                if epc:
                    abc = epc.get("abc_pct", 0)
                    mc  = epc.get("most_common", "—")
                    tot = epc.get("total", 0)
                    color = "🟢" if abc >= 60 else "🟡" if abc >= 30 else "🔴"
                    st.metric("A–C rated", f"{abc}% {color}", delta=None)
                    st.text(f"  Most common: {mc}")
                    st.text(f"  Certificates found: {tot}")
                    ratings = epc.get("ratings", {})
                    if ratings:
                        st.caption("  " + "  ·  ".join(f"{k}:{v}" for k,v in sorted(ratings.items())))
                else:
                    st.caption("EPC data not available for this postcode.")
            with flood_col:
                st.markdown("**🌊 Flood Risk**")
                flood_icon = {"Zone 3 (High)": "🔴", "Zone 2 (Medium)": "🟡",
                              "Zone 1 (Low)": "🟢"}.get(flood, "⚪")
                st.metric("Planning zone", f"{flood_icon} {flood}")
                if flood == "Zone 3 (High)":
                    st.caption("High probability of flooding. Resilience and continuity planning recommended.")
                elif flood == "Zone 2 (Medium)":
                    st.caption("Medium probability. Consider site resilience during sales conversations.")
                elif flood == "Zone 1 (Low)":
                    st.caption("Low flood risk. No specific constraints identified.")

        if flags:
            st.divider()
            st.markdown("**⚠️ Intelligence Flags**")
            for title, detail in flags:
                st.warning(f"**{title}** — {detail}")

        if ops:
            st.divider()
            st.markdown("**💼 Commercial Opportunities**")
            for i, op in enumerate(ops, 1):
                st.info(f"{i}. {op}")

        st.divider()
        with st.spinner("Building PDF..."):
            ws_data = {
                "wiredScore": {
                    "status":  ws_status.lower().replace(" ", "-") if ws_status != "Not verified" else "unconfirmed",
                    "scheme":  ws_scheme,
                    "level":   ws_level,
                },
                "smartScore": {
                    "status":  ss_status.lower().replace(" ", "-") if ss_status != "Not verified" else "unconfirmed",
                    "level":   ss_level,
                },
            }
            pdf_buf = generate_park_pdf(park, ofcom, companies, epc=epc, flood_risk=flood, ws_data=ws_data)

        fname = f"{park['name'].replace(' ','_').replace('/','_')}_intelligence_report.pdf"
        st.download_button("📥 Download Intelligence Report (PDF)", pdf_buf, file_name=fname,
                            mime="application/pdf", use_container_width=True, type="primary")
        export_data = build_export_data(park, ofcom, companies, "park", park.get("name",""),
                                        epc=epc, flood_risk=flood)
        export_json = json.dumps(export_data, indent=2, default=str)
        st.download_button(
            "📤 Export Data for Master Report",
            data=export_json,
            file_name=f"{park['name'].replace(' ','_')}_export.json",
            mime="application/json",
            use_container_width=True,
        )

# ─── AREA / MULTI-PARK MODE ───────────────────────────────────────────────────
else:
    if all_clusters_mode:
        area_label = selected_region_name
        report_title = f"Retail Digital Infrastructure Report: {selected_region_name}"
    else:
        area_label = f"{selected_cluster_name}, {selected_region_name}"
        report_title = f"Retail Digital Infrastructure Report: {selected_cluster_name}"

    parks_list = parks_in_scope
    for park in parks_list:
        park["_region"] = selected_region_name
        if not all_clusters_mode:
            park["_cluster"] = selected_cluster_name
        else:
            for c in selected_region["clusters"]:
                if any(p["id"] == park["id"] for p in c["assets"]):
                    park["_cluster"] = c["name"]

    st.subheader(f"📊 Retail Area Report: {area_label}")
    st.markdown(f"**{len(parks_list)} assets** will be profiled across this {'region' if all_clusters_mode else 'cluster'}.")

    with st.expander(f"View all {len(parks_list)} assets in scope", expanded=False):
        for p in parks_list:
            st.text(f"  • {p['name']} — {p.get('type','')} ({p.get('local_authority','')})")

    with st.expander("🏅 WiredScore / SmartScore — optional, enter known certification status before generating"):
        st.caption("Enter known statuses before generating. Leave blank for assets not yet verified. Check wiredscore.com/map")
        for p in parks_list:
            pid  = p["id"]
            pname= p["name"]
            c1, c2, c3, c4, c5 = st.columns([3, 2, 2, 2, 2])
            c1.markdown(f"**{pname[:30]}**")
            ws_s = c2.selectbox("WiredScore", ["—", "Certified", "Not certified"],
                                key=f"aws_{pid}", label_visibility="collapsed")
            ws_l = c3.selectbox("WS Level", ["—", "Certified", "Silver", "Gold", "Platinum"],
                                key=f"awsl_{pid}", label_visibility="collapsed") if ws_s == "Certified" else "—"
            ss_s = c4.selectbox("SmartScore", ["—", "Certified", "Not certified"],
                                key=f"ass_{pid}", label_visibility="collapsed")
            ss_l = c5.selectbox("SS Level", ["—", "Certified", "Silver", "Gold", "Platinum"],
                                key=f"assl_{pid}", label_visibility="collapsed") if ss_s == "Certified" else "—"

    # Show a quick summary of what WiredScore/SmartScore has been entered
    ws_summary_parts = []
    for p in parks_list:
        pid  = p["id"]
        ws_s = st.session_state.get(f"aws_{pid}", "—")
        ss_s = st.session_state.get(f"ass_{pid}", "—")
        if ws_s != "—" or ss_s != "—":
            ws_label = f"WS:{ws_s}" if ws_s != "—" else ""
            ss_label = f"SS:{ss_s}" if ss_s != "—" else ""
            parts = " · ".join(x for x in [ws_label, ss_label] if x)
            ws_summary_parts.append(f"{p['name'][:25]}: {parts}")
    if ws_summary_parts:
        st.caption("Certification entries recorded: " + "  |  ".join(ws_summary_parts))

    if st.button(f"🔍 Generate {area_label} Retail Intelligence Report", type="primary", use_container_width=True):
        with st.spinner(f"Pulling Ofcom data for {len(parks_list)} assets..."):
            all_ofcom = {}
            for park in parks_list:
                la = park.get("local_authority","")
                if la:
                    all_ofcom[park["id"]] = get_ofcom(la)
                else:
                    all_ofcom[park["id"]] = {}
        st.session_state["area_ofcom"]       = all_ofcom
        st.session_state["area_intelligence"]= None
        st.session_state["area_parks"]       = parks_list
        st.session_state["area_label"]       = area_label
        st.session_state["report_title"]     = report_title

    intel_label = "🔬 Run Full Intelligence (EPC · Companies House · Flood Risk)"
    if intelligence_available:
        if st.button(intel_label, use_container_width=True):
            progress = st.progress(0, text="Starting intelligence run…")
            all_intelligence = {}
            for i, park in enumerate(parks_list):
                progress.progress((i) / len(parks_list),
                                  text=f"Running intelligence for {park['name']} ({i+1}/{len(parks_list)})…")
                intel = run_park_intelligence(park, ch_api_key, epc_bearer_token)
                all_intelligence[park["id"]] = intel
            progress.progress(1.0, text="Intelligence run complete.")
            # Merge ofcom from full run into all_ofcom dict too
            all_ofcom = {pid: v["ofcom"] for pid, v in all_intelligence.items()}
            st.session_state["area_ofcom"]        = all_ofcom
            st.session_state["area_intelligence"]  = all_intelligence
            st.session_state["area_parks"]         = parks_list
            st.session_state["area_label"]         = area_label
            st.session_state["report_title"]       = report_title
    else:
        st.info("ℹ️ Add Companies House and EPC API keys in settings to enable Full Intelligence run.")

    # ── Results display ────────────────────────────────────────────────────────
    if "area_ofcom" not in st.session_state or st.session_state.get("area_label") != area_label:
        st.stop()

    all_ofcom       = st.session_state["area_ofcom"]
    all_intelligence= st.session_state.get("area_intelligence")
    parks_list      = st.session_state["area_parks"]
    area_label      = st.session_state["area_label"]
    report_title    = st.session_state["report_title"]
    # Build area_ws directly from widget session_state — always current, no snapshot needed
    def _build_area_ws(pl):
        d = {}
        for p in pl:
            pid  = p["id"]
            ws_s = st.session_state.get(f"aws_{pid}", "—")
            ws_l = st.session_state.get(f"awsl_{pid}", "—")
            ss_s = st.session_state.get(f"ass_{pid}", "—")
            ss_l = st.session_state.get(f"assl_{pid}", "—")
            d[pid] = {
                "wiredScore": {
                    "status": ws_s.lower().replace(" ", "-") if ws_s not in ("—", "") else "unconfirmed",
                    "level":  ws_l if ws_l not in ("—", "") else "",
                },
                "smartScore": {
                    "status": ss_s.lower().replace(" ", "-") if ss_s not in ("—", "") else "unconfirmed",
                    "level":  ss_l if ss_l not in ("—", "") else "",
                },
            }
        return d
    area_ws = _build_area_ws(st.session_state.get("area_parks", parks_list))

    # TEMP DEBUG — remove after testing
    with st.expander("🔧 Debug: WiredScore data captured"):
        for pid, v in area_ws.items():
            ws = v.get("wiredScore",{}).get("status","?")
            ss = v.get("smartScore",{}).get("status","?")
            st.text(f"ID:{pid}  WS:{ws}  SS:{ss}")

    if all_intelligence:
        st.success(f"✅ Full intelligence run complete — {len(all_intelligence)} assets enriched with EPC, Companies House, and flood risk data.")

    with_data    = [(p, all_ofcom.get(p["id"])) for p in parks_list if all_ofcom.get(p["id"])]
    scored       = [(p, o, score_connectivity(o)[0]) for p, o in with_data]
    scored_valid = [(p, o, s) for p, o, s in scored if s is not None]

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Assets profiled", len(parks_list))
    if scored_valid:
        avg    = round(sum(s for _,_,s in scored_valid) / len(scored_valid))
        greens = sum(1 for _,_,s in scored_valid if s >= 70)
        m2.metric("Avg connectivity score", f"{avg}/100")
        m3.metric("Assets with Ofcom data",  len(scored_valid))
        m4.metric("Green RAG", f"{greens}/{len(scored_valid)}")
    else:
        m2.metric("Avg connectivity score", "—")
        m3.metric("Assets with Ofcom data",  "0")
        m4.metric("Green RAG", "—")

    if all_intelligence:
        # Extra summary metrics from full run
        all_companies = [all_intelligence[p["id"]].get("companies", [])
                         for p in parks_list if p["id"] in all_intelligence]
        total_active  = sum(
            sum(1 for c in co if c.get("company_status","").lower() == "active")
            for co in all_companies
        )
        high_flood    = sum(
            1 for p in parks_list
            if (all_intelligence.get(p["id"]) or {}).get("flood_risk","") == "Zone 3 (High)"
        )
        epc_data      = [all_intelligence[p["id"]].get("epc",{})
                         for p in parks_list if p["id"] in all_intelligence]
        abc_vals      = [e["abc_pct"] for e in epc_data if e.get("abc_pct") is not None]
        m5, m6, m7    = st.columns(3)
        m5.metric("Active companies (total)", total_active)
        m6.metric("High flood risk assets",    high_flood)
        m7.metric("Avg EPC A–C %",
                  f"{round(sum(abc_vals)/len(abc_vals))}%" if abc_vals else "—")

    st.divider()

    st.markdown("**📡 Connectivity Comparison — Ranked**")
    ranked = sorted(scored_valid, key=lambda x: -x[2])
    for park, ofcom, conn_score in ranked:
        rag      = score_connectivity(ofcom)[1]
        rag_icon = {"Green": "🟢", "Amber": "🟡", "Red": "🔴"}.get(rag, "⚪")
        ff       = f"{ofcom.get('full_fibre_pct',0):.0f}%"
        g5       = f"{ofcom.get('outdoor_5g_pct',0):.0f}%"
        if all_intelligence:
            intel  = all_intelligence.get(park["id"], {})
            flood  = intel.get("flood_risk","—")
            epc    = intel.get("epc", {})
            epc_str= f"EPC:{epc.get('most_common','—')}" if epc else "—"
            flood_icon = {"Zone 3 (High)":"🔴","Zone 2 (Medium)":"🟡","Zone 1 (Low)":"🟢"}.get(flood,"⚪")
            cos    = sum(1 for c in intel.get("companies",[]) if c.get("company_status","").lower()=="active")
            col_a,col_b,col_c,col_d,col_e,col_f,col_g = st.columns([3,1.5,1.2,1.2,1.2,1,1.5])
            col_a.text(park["name"][:35])
            col_b.markdown(f"{rag_icon} **{conn_score}/100**")
            col_c.text(f"FF:{ff}")
            col_d.text(f"5G:{g5}")
            col_e.text(epc_str)
            col_f.markdown(f"{flood_icon}")
            col_g.text(f"🏢{cos} cos")
        else:
            col_a,col_b,col_c,col_d,col_e = st.columns([3,1.5,1.2,1.2,1.5])
            col_a.text(park["name"][:38])
            col_b.markdown(f"{rag_icon} **{conn_score}/100**")
            col_c.text(f"FF: {ff}")
            col_d.text(f"5G: {g5}")
            col_e.text(park.get("type","")[:20])

    no_data_parks = [p for p in parks_list if not all_ofcom.get(p["id"])]
    if no_data_parks:
        with st.expander(f"{len(no_data_parks)} assets without Ofcom data"):
            for p in no_data_parks:
                st.text(f"  • {p['name']} (LA: {p.get('local_authority','')})")

    st.divider()

    all_ops = {}
    for park in parks_list:
        ofcom    = all_ofcom.get(park["id"]) or {}
        companies= (all_intelligence or {}).get(park["id"], {}).get("companies", [])
        pid_s    = park.get("id","")
        pws_s    = area_ws.get(pid_s, {})
        for op in generate_opportunities(park, ofcom, companies, ws_data=pws_s if pws_s else None):
            all_ops[op] = all_ops.get(op, 0) + 1
    if all_ops:
        st.markdown("**💼 Top Opportunities Across Area**")
        for op, count in sorted(all_ops.items(), key=lambda x: -x[1])[:6]:
            st.info(f"**{count} assets** — {op}")

    # ── Full intelligence: EPC & flood summary table ───────────────────────────
    if all_intelligence:
        st.divider()
        st.markdown("**⚡🌊 EPC & Flood Risk Summary**")
        for park in parks_list:
            intel  = all_intelligence.get(park["id"], {})
            epc    = intel.get("epc", {})
            flood  = intel.get("flood_risk", "—")
            flood_icon = {"Zone 3 (High)":"🔴","Zone 2 (Medium)":"🟡","Zone 1 (Low)":"🟢"}.get(flood,"⚪")
            epc_str= f"{epc.get('abc_pct','—')}% A–C  (most common: {epc.get('most_common','—')})" if epc else "No EPC data"
            col1, col2, col3 = st.columns([3, 2, 2])
            col1.text(park["name"][:38])
            col2.text(f"EPC: {epc_str[:30]}")
            col3.text(f"{flood_icon} {flood}")

    st.divider()

    with st.spinner("Building area report PDF..."):
        pdf_buf = generate_area_pdf(area_label, parks_list, all_ofcom, report_title, all_intelligence=all_intelligence, area_ws=area_ws)

    safe_name = area_label.replace(" ","_").replace("&","and").replace("–","_").replace("/","_")
    fname     = f"{safe_name}_area_report.pdf"
    st.download_button("📥 Download Area Report (PDF)", pdf_buf, file_name=fname,
                       mime="application/pdf", use_container_width=True, type="primary")

    export_data = build_export_data(
        None, None, None, "area", area_label,
        parks_list=parks_list, all_ofcom=all_ofcom,
        all_intelligence=all_intelligence
    )
    export_json = json.dumps(export_data, indent=2, default=str)
    intel_suffix = "_enriched" if all_intelligence else ""
    st.download_button(
        f"📤 Export {area_label} Retail Data for Master Report",
        data=export_json,
        file_name=f"{safe_name}{intel_suffix}_export.json",
        mime="application/json",
        use_container_width=True,
    )
    if all_intelligence:
        st.caption("✅ Export includes EPC, Companies House, and flood risk data for every asset.")
    else:
        st.caption("ℹ️ Run Full Intelligence above to enrich the export with EPC, Companies House, and flood risk data.")

    st.divider()
    st.markdown("**🔎 Drill into individual assets from this area**")
    st.info("Use the selectors above to pick an individual asset for a detailed report.")
