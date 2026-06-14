# Environment Variables (Disaster Recovery Pack)

The following environment variables are required for the system to function correctly. 
They must be set in the local `.env` file and configured in the Vercel project settings.

## Supabase (Database & Auth)
- `SUPABASE_URL`: The URL of the Supabase project (e.g., `https://fvkvfekzoebcolssnteo.supabase.co`).
- `SUPABASE_ANON_KEY`: The public anonymous key for frontend API access.
- `SUPABASE_SERVICE_ROLE_KEY`: The secret service role key for backend Python scripts (bypasses RLS).

## Data Providers
- `EDINET_API_KEY`: API key for fetching data from EDINET.
- `JQUANTS_API_KEY`: Authentication token for the J-Quants API.
- `JQUANTS_MAIL_ADDRESS`: Registered email for J-Quants.
- `JQUANTS_PASSWORD`: Password for J-Quants.

## Notifications & AI
- `DISCORD_WEBHOOK_URL`: Webhook URL for posting alerts to Discord.
- `OPENAI_API_KEY`: OpenAI API key used for PDF text extraction and LLM parsing.

## System Configuration
- `ALERT_YOY_PCT`: Threshold percentage for YoY alerts (e.g., `30`).
- `ALERT_YOQ_PCT`: Threshold percentage for QoQ alerts (e.g., `30`).
- `ENABLE_EARNINGS_V2_PIPELINE`: Feature flag (`1` or `0`) to enable the V2 earnings ingestion pipeline.
