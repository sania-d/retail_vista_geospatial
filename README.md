# Retail Vista V5 - Hierarchical Multi-Agent Spatial Intelligence

Retail Vista V5 is a state-of-the-art, hierarchical multi-agent spatial intelligence platform designed to solve complex geocoding, catchment analysis, route optimization, and retail asset intelligence tasks in real time.

Built on top of the **Google Agent Development Kit (ADK)**, the system orchestrates a team of specialized sub-agents and advanced custom tools to calculate drive-times, fetch spatial building indices, discover competitor locations, and generate strategic business insights.

---

## 🚀 Key Features

*   **Hierarchical Multi-Agent Coordination**: Uses a central coordinator (`main_agent`) to delegate complex business tasks to specialized sub-agents (`catchment_agent`, `competitor_agent`, `feasibility_agent`).
*   **Parallel Route Calculations**: Integrates with the **Google Maps Routes API (v2)** to compute exact driving/walking times and road distances in parallel across multiple catchment polygon vertices.
*   **Flexible Spatial Geofencing**: Dynamically generates circular and pincode-based catchment polygons.
*   **H3 Hexagonal Grid Aggregation**: Maps geographic data onto the global H3 grid system for vectorized spatial overlays, building density indexing, and wealth indexing.
*   **Robust Dual-Platform Architecture**:
    *   **Cloud Run (High-Fidelity Mode)**: Runs inside custom Docker containers with pre-compiled GIS C-libraries (`libgdal`/`libgeos` for `geopandas`/`shapely`) to serve public web APIs.
    *   **Vertex AI Agent Engine (Serverless Fallback)**: Gracefully falls back to pure-Python mathematical distance and bearing formulas when system C-libraries are absent, enabling native registration and debugging inside Google Cloud Console!

---

## 🏗️ Team of Specialized Sub-Agents

1.  **Catchment Agent**: Computes driving times and road distances around prospective retail locations, resolving geographic vertices to calculate precise road metrics.
2.  **Competitor Agent**: Conducts comparative analysis of retail assets, scanning competitor locations and footprints against target trade areas to find whitespace opportunities.
3.  **Feasibility Agent**: Aggregates spatial variables (such as building density and relative wealth index) inside BigQuery, mapping them to H3 grid cells to qualify market viability.

---

## ⚙️ Setup & Installation

### 1. Clone & Navigate
```bash
git clone https://github.com/sania-d/retail_vista_geospatial.git
cd retail_vista_geospatial
```

### 2. Environment Configuration
Create a `.env` file in the root directory:
```ini
PROJECT_ID=YOUR_GOOGLE_CLOUD_PROJECT_ID
DATASET_ID=YOUR_BIGQUERY_DATASET_ID
MODEL=gemini-2.5-pro
MAPS_API_KEY=YOUR_GOOGLE_MAPS_API_KEY
```

---

## ☁️ Deployment Options

### Option A: Google Cloud Run (Docker)
To build and deploy the custom high-fidelity container image remotely using **Google Cloud Build**, simply run:
```bash
chmod +x deploy_sandaw.sh
./deploy_sandaw.sh
```

### Option B: Vertex AI Agent Engine (Native)
To package and register your agent natively inside the Google Cloud Console Playground:
```bash
# Run the native ADK deployment
python3 -m google.adk.cli deploy agent_engine \
    --project="YOUR_GOOGLE_CLOUD_PROJECT_ID" \
    --region="us-central1" \
    --display_name="Retail Vista Agent" \
    --requirements_file="requirements_reasoning.txt" \
    .
```
