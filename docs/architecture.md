# Architecture (Disaster Recovery Pack)

## System Overview
The TDNET Alerts & Company Viewer is a full-stack application designed to aggregate, parse, and visualize financial disclosures.

### Core Components
1. **Frontend (Next.js)**:
   - Hosted on Vercel.
   - Provides `/tdnet-alerts` UI and Company Viewer spread-sheet interface.
2. **Backend / Database (Supabase PostgreSQL)**:
   - Serves as the primary data store for `tdnet_events`, `financials`, `canonical_financials`.
   - Utilizes PostgREST for direct API querying from the frontend.
3. **Data Pipeline (Python)**:
   - Scheduled tasks to fetch events from TDNET.
   - LLM/OCR to extract data from PDFs.
   - Parsing XBRL files.
   - Synchronization with J-Quants API.

### Data Flow
1. **Ingestion**:
   `TDNET RSS / API` -> Python Crawler -> Extracts Event Data -> Inserts to `tdnet_events`
2. **Extraction**:
   Python Crawler -> Downloads PDF/XBRL -> Parses XBRL or uses LLM for PDF -> Saves JSON to `raw_payload`
3. **Normalization**:
   Python Processor -> Reads `raw_payload` -> Normalizes into `canonical_financials` (resolves period, sets base units).
4. **Presentation Prep**:
   Python Processor -> Joins and formats -> Upserts to `financials` table.
5. **UI Rendering**:
   Next.js -> Fetches `tdnet_events` -> Generates YOY JSON (`notification_compare_json`) -> Renders Alert Cards.
