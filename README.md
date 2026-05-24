# Amazon EBS Cold Storage Evaluator

This tool evaluates whether EBS snapshots should be transitioned to the **EBS Snapshots Archive (Cold) Tier** by analysing at the **volume level** — not individual snapshots. It accounts for the block re-attribution behaviour that makes per-snapshot analysis misleading.

## Why Volume-Level Analysis?

Most existing tools (including the archived [amazon-ebs-snapshot-tier-evaluator](https://github.com/aws-samples/amazon-ebs-snapshot-tier-evaluator)) evaluate individual snapshots in isolation. This produces **incorrect recommendations** because of how EBS snapshot lineages work:

### The Block Re-Attribution Problem

When you archive a single snapshot from a lineage:

```
Snap A → Snap B → Snap C  (warm tier, incremental)
```

If you archive **Snap B**:
- Snap B becomes a **full snapshot** in cold tier (charged at $0.0125/GB)
- The blocks that Snap B shared with Snap C get **re-attributed to Snap C** in warm tier
- Warm tier cost **does not decrease** — it shifts to remaining snapshots
- **Net result: Warm cost stays the same + Cold cost added = INCREASE**

This tool evaluates the **entire volume's snapshot lineage** to determine if cold storage makes sense holistically.

---

## The 25% Rule of Thumb

Archive tier costs 1/4 of Standard tier per GB, but stores the **full snapshot** (not incremental):

| Scenario | Standard Cost | Archive Cost | Verdict |
|----------|:---:|:---:|:---:|
| Incremental snapshot < 25% of volume | Lower | Higher | ❌ Keep in Standard |
| Incremental snapshot > 25% of volume | Higher | Lower | ✅ Archive saves money |
| Incremental snapshot = 25% of volume | Equal | Equal | Break-even |

**However**, this rule only applies to **isolated snapshots**. For snapshots within a lineage, the re-attribution problem means you must archive ALL snapshots of a volume together, or none.

---

## Decision Logic

The evaluator analyses each volume and assigns one of the following recommendations:

| Recommendation | Colour | Criteria |
|---|---|---|
| **COLD STORAGE CANDIDATE** | 🟢 Green | Volume decommissioned (no longer exists) OR single standalone snapshot OR high-churn volume (>20% monthly change rate) |
| **COLD STORAGE CANDIDATE (HIGH CHURN)** | 🟢 Green | Volume exists with >20% monthly write change rate — monthly snapshots accumulate unique blocks quickly |
| **BORDERLINE - REVIEW** | 🟡 Yellow | Volume has 15-20% monthly change rate — may break even, recommend monitoring |
| **NOT RECOMMENDED** | 🔴 Red | Volume has <15% monthly change rate — archiving would likely increase costs |
| **NOT ELIGIBLE** | 🔴 Red | Snapshots expire within 90 days (archive minimum retention) |
| **HOUSEKEEP - DELETE ALL** | 🟠 Orange | All snapshots have expired retention — delete to save costs immediately |
| **HOUSEKEEP FIRST** | 🟠 Orange | Mix of expired and active snapshots — clean up expired first |
| **INSUFFICIENT DATA** | Grey | No CloudWatch metrics available to determine change rate |

### Decision Flowchart

```
┌─────────────────────────────────────┐
│ For each EBS Volume with snapshots  │
└──────────────┬──────────────────────┘
               │
               ▼
┌──────────────────────────────────┐     YES    ┌─────────────────────────┐
│ All snapshots expired?           │────────────▶│ HOUSEKEEP - DELETE ALL  │
└──────────────┬───────────────────┘             └─────────────────────────┘
               │ NO
               ▼
┌──────────────────────────────────┐     YES    ┌─────────────────────────┐
│ Volume decommissioned?           │────────────▶│ COLD STORAGE CANDIDATE  │
│ (no longer exists)               │             │ (no re-attribution)     │
└──────────────┬───────────────────┘             └─────────────────────────┘
               │ NO
               ▼
┌──────────────────────────────────┐     YES    ┌─────────────────────────┐
│ Single standalone snapshot?      │────────────▶│ COLD STORAGE CANDIDATE  │
│ (no lineage)                     │             │ (saves 75%)             │
└──────────────┬───────────────────┘             └─────────────────────────┘
               │ NO
               ▼
┌──────────────────────────────────┐
│ Check CloudWatch VolumeWriteBytes│
│ (30-day change rate)             │
└──────────────┬───────────────────┘
               │
        ┌──────┼──────────┐
        │      │          │
        ▼      ▼          ▼
    >20%    15-20%      <15%
      │        │          │
      ▼        ▼          ▼
┌──────────┐ ┌─────────┐ ┌───────────────┐
│ CANDIDATE│ │BORDERLINE│ │NOT RECOMMENDED│
│(HIGH     │ │- REVIEW  │ │(would increase│
│ CHURN)   │ │          │ │ costs)        │
└──────────┘ └─────────┘ └───────────────┘
```

---

## When Cold Storage Tiering Makes Sense

### ✅ Recommended Scenarios

1. **Volume Decommissioned / Backup Stopped**
   - No new daily/weekly snapshots being created
   - The snapshot(s) hold all data uniquely — no block sharing
   - Cold saves up to 75% unconditionally
   - *This is the strongest use case*

2. **Single Standalone Snapshot (No Lineage)**
   - Only one snapshot exists for the volume
   - No other snapshots to re-attribute blocks to
   - Archive saves 75% of storage cost

3. **High-Churn Volumes (>20% Monthly Change Rate)**
   - Database servers, build servers, CI/CD volumes
   - Blocks are frequently overwritten
   - Monthly snapshots accumulate unique blocks quickly
   - Cold becomes cheaper within a few months
   - **Note:** Only archive monthly snapshots; keep daily/weekly in standard

4. **Compliance / End-of-Project Snapshots**
   - One-off snapshots taken for audit purposes
   - Will never have newer snapshots in the lineage
   - Standalone full snapshots — cold saves 75%

5. **Very Long Retention (7+ Years)**
   - Even at moderate change rates (15-20%), cumulative warm cost over 7 years is substantial
   - The absolute dollar savings justify cold tiering for borderline cases

### ❌ Not Recommended Scenarios

1. **Low-Write Volumes (<15% Monthly Change Rate)**
   - Static web servers, file servers
   - Archiving converts incremental to full snapshot — costs MORE
   - Block re-attribution means warm cost stays the same

2. **Active Backup Lineages (Daily + Weekly + Monthly)**
   - Archiving one snapshot shifts its blocks to neighbours
   - Net cost increases unless ALL snapshots are archived together

3. **Append-Only Workloads**
   - New blocks added but old blocks rarely overwritten
   - Incremental snapshots stay small — standard tier is cheaper

4. **Snapshots Expiring Within 90 Days**
   - Archive tier charges minimum 90 days regardless
   - If snapshot will be deleted sooner, you pay for unused retention

---

## Cost Comparison Example

**1 TB volume, 10% monthly change rate, 14-month retention:**

| Tier | Calculation | Cost |
|------|-------------|------|
| Standard (warm) | Growing unique blocks over 14 months | ~$150-$180 |
| Archive (cold) | 1024 GiB × $0.0125 × 14 months | $179.20 |
| **Verdict** | | **Break-even / marginal** |

**1 TB volume, 25% monthly change rate, 14-month retention:**

| Tier | Calculation | Cost |
|------|-------------|------|
| Standard (warm) | Growing unique blocks over 14 months | ~$350-$400 |
| Archive (cold) | 1024 GiB × $0.0125 × 14 months | $179.20 |
| **Verdict** | | **Cold saves ~50%** |

---

## How to Use

### Prerequisites

- Python 3.9+
- AWS credentials configured (profile or environment variables)
- IAM permissions: `ec2:DescribeSnapshots`, `ec2:DescribeVolumes`, `ebs:ListChangedBlocks`, `ebs:ListSnapshotBlocks`, `cloudwatch:GetMetricStatistics`, `backup:ListBackupVaults`, `backup:ListRecoveryPointsByBackupVault`, `pricing:GetProducts`, `ssm:GetParameter`

### Installation

```bash
git clone https://github.com/<your-username>/ebs-cold-storage-evaluator.git
cd ebs-cold-storage-evaluator
pip install -r requirements.txt
```

### Execution

```bash
# Basic usage
python ebs_cold_storage_evaluator.py <aws-profile> <region>

# Examples
python ebs_cold_storage_evaluator.py default us-east-1
python ebs_cold_storage_evaluator.py production eu-west-1
python ebs_cold_storage_evaluator.py myprofile ap-southeast-2
```

### Output

The script generates:
1. **CSV file** — machine-readable results
2. **Excel file (.xlsx)** — colour-coded with two worksheets:
   - **Sheet 1: Cold Storage Assessment** — volume-level recommendations
   - **Sheet 2: Snapshot Detail** — individual snapshot metadata for reference

### Output Columns

| Column | Description |
|--------|-------------|
| `region` | AWS region |
| `volume_id` | EBS volume ID |
| `volume_size_gb` | Volume size in GiB |
| `volume_exists` | Whether the source volume still exists |
| `total_snapshots` | Number of snapshots for this volume |
| `oldest_snapshot_id` | ID of the oldest snapshot |
| `oldest_snapshot_date` | Creation date of the oldest snapshot |
| `newest_snapshot_id` | ID of the most recent snapshot |
| `newest_snapshot_date` | Creation date of the newest snapshot |
| `snapshot_span_days` | Days between oldest and newest snapshot |
| `monthly_change_rate_pct` | Estimated monthly write change rate from CloudWatch |
| `write_bytes_30d_gb` | Total bytes written in last 30 days |
| `newest_full_snapshot_gb` | Full snapshot size (what archive tier would store) |
| `expired_snapshots` | Count of snapshots past their retention date |
| `active_expiry_snapshots` | Count of snapshots with future expiry dates |
| `min_days_to_expiry` | Shortest time until a snapshot expires |
| `max_retention_days` | Longest retention policy configured |
| `warm_cost_per_month_est` | Estimated monthly cost in Standard tier |
| `cold_cost_per_month_est` | Estimated monthly cost in Archive tier |
| `recommendation` | Volume-level recommendation |
| `decision_reason` | Detailed explanation of the recommendation |

---

## How It Measures Change Rate

The script uses **CloudWatch VolumeWriteBytes** (30-day sum) as a proxy for monthly change rate:

```
Monthly Change Rate = (VolumeWriteBytes_30d / Volume_Size) × 100%
```

**Important:** VolumeWriteBytes includes repeated overwrites to the same blocks, so the actual unique-block change rate is lower. This makes the estimate conservative — if the script says >20%, the true unique change rate is likely lower, meaning cold storage is even more beneficial than estimated.

For more precise measurement, use the EBS Direct API:

```bash
aws ebs list-changed-blocks \
  --first-snapshot-id snap-monthly \
  --second-snapshot-id snap-latest-daily
```

Changed blocks × 512 KiB = blocks overwritten since the monthly snapshot was taken.

---

## Assumptions & Key Notes

- Pricing is based on GB-month (30-day month)
- Archive tier has a **minimum 90-day retention** — snapshots deleted before 90 days incur pro-rated charges
- Archive tier restoration takes **up to 72 hours** depending on snapshot size
- The script calls AWS Pricing API (us-east-1) for dynamic regional pricing
- CloudWatch metrics require the volume to be attached and have monitoring enabled
- The `ListChangedBlocks` and `ListSnapshotBlocks` APIs have associated costs — review [EBS Pricing](https://aws.amazon.com/ebs/pricing/)

---

## Architecture

This is a **standalone Python script** — no infrastructure deployment required. It uses:

- **EC2 API** — `DescribeSnapshots`, `DescribeVolumes`
- **EBS Direct API** — `ListChangedBlocks`, `ListSnapshotBlocks`
- **CloudWatch API** — `GetMetricStatistics` (VolumeWriteBytes)
- **AWS Backup API** — `ListBackupVaults`, `ListRecoveryPointsByBackupVault`
- **Pricing API** — `GetProducts` (dynamic pricing lookup)
- **SSM API** — `GetParameter` (region name resolution)

---

## References

- [AWS Documentation: Archive Amazon EBS Snapshots](https://docs.aws.amazon.com/ebs/latest/userguide/snapshot-archive.html)
- [AWS Documentation: Guidelines and Best Practices for Archiving](https://docs.aws.amazon.com/ebs/latest/userguide/archiving-guidelines.html)
- [AWS Documentation: Determining the Reduction in Standard Tier Storage Costs](https://docs.aws.amazon.com/ebs/latest/userguide/archiving-guidelines.html#archive-guidelines)
- [AWS Blog: New Amazon EBS Snapshots Archive](https://aws.amazon.com/blogs/aws/new-amazon-ebs-snapshots-archive/)
- [EBS Pricing](https://aws.amazon.com/ebs/pricing/)

---

## Security

See [CONTRIBUTING](CONTRIBUTING.md) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
