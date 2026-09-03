Cloud Crucix Community Edition
===============================

A read-only BigQuery activity dashboard — who queries what, who gets denied,
and what it costs — from job metadata only, running on your own machine.

This is the **Community Edition**: a free preview of Cloud Crucix with the
Activity tab fully working. Upgrade to the Full Edition to unlock the complete
suite of cost analysis, workload insights, and PDF reporting.


WHAT'S INCLUDED (Community Edition)
-------------------------------------
Activity tab — fully working:
  - Overview stat cards with detailed hover breakdowns
  - Top Users ranked by job count and bytes processed
  - Top Tables ranked by query activity
  - Daily Spend chart with spike-day detection
  - Permission Errors from job history
  - Failure Reasons breakdown
  - Activity Heatmap (day × hour)
  - Search across users and tables
  - Project / region auto-discovery
  - Cloud Billing Catalog live pricing
  - Setup panel with identity, diagnostics, and self-cost measurement
  - Auto-refresh (hourly by default, adjustable)

Premium tabs (Workload, Cost & Storage, Findings & Report) are shown as
promotional placeholders — no premium code is shipped.


WHAT'S IN THE FULL EDITION
-----------------------------
Workload tab:
  - Scheduled Queries — run history, cost, and failure rate
  - Most Active Tables — write-heavy tables driving ingestion cost
  - Anomaly Detection — users whose activity spikes ≥100× their median

Cost & Storage tab:
  - Cost Ranking — per-user and per-table query spend with live pricing
  - Storage Breakdown — table-level storage costs, logical vs physical billing
  - Savings Estimate — projected monthly saving from billing model switch

Findings & Report tab:
  - 8 risk findings: SELECT *, missing partition filters, cross-region queries,
    repeated queries, zombie tables, failed spend, unpartitioned tables
  - Cost Attribution — trace every GB and dollar to user, table, and query
  - Partition/Cluster Inventory — table layout analysis
  - PDF + JSON Export — full report ready to share

Additional features:
  - IAM Policy viewer — see who holds which roles
  - PAM Entitlements — privileged access manager overview
  - Audit Log denials — permission errors invisible to job history
  - Extended search — query text, scheduled queries, and more
  - Configurable analysis thresholds and risk settings


QUICK START
-------------
1. Install Docker Desktop (or Python 3.10+)
2. Place a service-account JSON key in the secrets/ folder
3. Double-click RUN-ME.bat (or run docker build + docker run)
4. Open http://localhost:5006

See RUN-ME.txt for full instructions.


GETTING THE FULL EDITION
---------------------------
Contact us for the Full Edition to unlock all features:

  - All 4 tabs with full data
  - Deep analysis engine with 8 risk checks
  - PDF and JSON report export
  - IAM/PAM/audit-log integration
  - Configurable thresholds and analysis settings
  - No time-window limitations


REQUIREMENTS
--------------
- Docker Desktop (or Python 3.10+)
- A Google Cloud service account with BigQuery read access
- See PERMISSIONS.txt for the exact roles needed


TECHNICAL DETAILS
-------------------
- Backend: Python Flask (bridge.py)
- Frontend: Single-file HTML/JS/SPA (ui.html)
- Database: None — reads BigQuery INFORMATION_SCHEMA metadata only
- Auth: Service account JSON key (no gcloud, no ADC)
- Network: All queries run locally, nothing uploaded
- License: Proprietary — see LICENSE.txt
