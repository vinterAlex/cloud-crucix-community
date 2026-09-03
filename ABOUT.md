# Cloud Crucix Community Edition

A read-only console over a BigQuery project. It shows who is querying what and
where the money goes — from job metadata only, running on your own machine.

It reads only *metadata* about the jobs that ran. It never reads the contents of
your tables and never writes anything.

This is the **Community Edition**: the Activity tab is fully working. Upgrade to
the Full Edition to unlock workload insights, cost analysis, and PDF reports.

## What's included (Activity tab)

- Overview stat cards with detailed hover breakdowns
- Top Users ranked by job count and bytes processed
- Top Tables ranked by query activity
- Daily Spend chart with spike-day detection
- Permission Errors from job history
- Failure Reasons breakdown
- Activity Heatmap (day x hour)
- Search across users and tables
- Project / region auto-discovery
- Cloud Billing Catalog live pricing
- Auto-refresh (hourly by default)

## What's in the Full Edition

### Workload tab
- Scheduled Queries — run history, cost, and failure rate
- Most Active Tables — write-heavy tables driving ingestion cost
- Anomaly Detection — users whose activity spikes 100x+ their median

### Cost & Storage tab
- Cost Ranking — per-user and per-table query spend with live pricing
- Storage Breakdown — table-level storage costs, logical vs physical billing
- Savings Estimate — projected monthly saving from billing model switch

### Findings & Report tab
- 8 risk findings: SELECT *, missing partition filters, cross-region queries,
  repeated queries, zombie tables, failed spend, unpartitioned tables
- Cost Attribution — trace every GB and dollar to user, table, and query
- Partition/Cluster Inventory — table layout analysis
- PDF + JSON Export — full report ready to share

### Additional
- IAM Policy viewer — see who holds which roles, including via groups
- PAM Entitlements — privileged access manager overview
- Audit Log denials — permission errors invisible to job history
- Extended search — query text, scheduled queries, and more
- Configurable analysis thresholds and risk settings
- No time-window limitations

## Security

- **Cannot read your data.** Job metadata only.
- **Does not ask for data access.** The BigQuery role for reading table rows
  (`dataViewer`) is deliberately excluded.
- **Runs as a service account you issue.** No gcloud, no personal credentials.
- **Nothing leaves the machine.** Local only, reachable from that machine alone.

## What it costs to run

6 queries per refresh on the default tab. At hourly refresh, roughly
**$0.30/month**. The Setup panel measures the real figure.

## To run it

Docker Desktop, and a read-only service-account key in the `secrets` folder.
Then double-click RUN-ME.bat. See RUN-ME.txt.
