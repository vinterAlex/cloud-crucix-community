# Cloud Crucix (Community Edition)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE.txt)
[![Docker Ready](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](#quick-start)
[![GCP BigQuery](https://img.shields.io/badge/GCP-BigQuery-4285F4?logo=googlecloud&logoColor=white)](#requirements)

A read-only BigQuery activity dashboard — who queries what, who gets denied access, and what it costs — derived entirely from local job metadata. 

> **Community Edition Note:** This repository contains a free preview with the **Activity** tab fully operational. Upgrade to the **Full Edition** to unlock complete cost auditing, workload insights, and PDF reporting.

---

## ⚡ What's Included vs. Full Edition

| Feature / Capability | Community Edition | Full Edition |
| :--- | :---: | :---: |
| **Activity Tab** (Stats, Top Users/Tables, Heatmap, Spikes) | ✅ | ✅ |
| **Project / Region Auto-Discovery & Live Pricing** | ✅ | ✅ |
| **Workload Insights** (Scheduled Jobs, Ingestion, Anomaly Detection) | ❌ | ✅ |
| **Cost & Storage Economics** (Logical vs. Physical Savings) | ❌ | ✅ |
| **8 Automated Waste Scanners** (SELECT *, Cache, Missing Partitions) | ❌ | ✅ |
| **Security & Auditing** (IAM Viewer, PAM, Audit Log Denials) | ❌ | ✅ |
| **1-Click Exports** (PDF & JSON Diagnostic Reports) | ❌ | ✅ |

---

## 📸 Screenshots

![Cloud Crucix Dashboard Main View](https://github.com/user-attachments/assets/7a011e88-c79c-4245-8a46-e0212cc74a29)

![Cloud Crucix Analytics View](https://github.com/user-attachments/assets/1144c820-83ac-499c-b956-86026fd6d697)

---

## 💎 What's Included in Community Edition

The **Activity** tab is fully unlocked out of the box:

* **Overview Stat Cards:** High-level metrics with detailed hover breakdowns.
* **Ranked Activity:** Top Users by job count/bytes processed & Top Tables by query count.
* **Daily Spend & Spikes:** Visual spend timeline with automatic spike-day detection.
* **Failure Analysis:** Permission errors from job history and failure reasons breakdown.
* **Activity Heatmap:** Interactive $Day \times Hour$ query activity visualization.
* **Metadata Diagnostics:** Search across users/tables, auto-refresh, and live catalog pricing.

*(Premium tabs like Workload, Cost & Storage, and Findings are displayed as promotional placeholders).*

---

## 🚀 Upgrade to the Full Edition

Unlock the complete cost-analysis, workload attribution, and security auditing engine.

**Price:** **~$330 USD** — *One-time purchase, perpetual license, no subscription.*

👉 **[Buy Cloud Crucix Full Edition on LemonSqueezy](https://bigquery-cost-report.lemonsqueezy.com/checkout/buy/a1d38055-8720-4f65-bfcb-05c8606389ca)**

> 🚨 **Launch Special:** Use promo code **`CRUCIX25`** for **25% OFF** *(First 10 buyers only)*.

---

## 🛠️ Quick Start

Requires **Docker Desktop** (installed and running) and a **Google Service Account Key** with read access to BigQuery job metadata. Zero infrastructure required.

### Option 1: Docker (Recommended)

1. **Initialize Setup**  
   Double-click `RUN-ME.bat` (Windows) or execute `bash run-me.sh` (macOS/Linux). This automatically creates a `secrets/` directory.

2. **Add Your Credentials**  
   Place your GCP service account JSON key into the `secrets/` directory:
   ```text
   cloud-crucix-community/secrets/my-service-account.json
