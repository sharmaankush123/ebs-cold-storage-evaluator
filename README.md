# Amazon EBS Cold Storage Evaluator

This tool evaluates whether EBS snapshots should be transitioned to the **EBS Snapshots Archive (Cold) Tier** by analysing at the **volume level** — not individual snapshots. It uses the **EBS Direct API** (`ListChangedBlocks` / `ListSnapshotBlocks`) to measure actual block-level data, applying a single **25% threshold** for unambiguous recommendations.

## Why Volume-Level Analysis?

Most existing tools evaluate individual snapshots in isolation. This produces **incorrect recommendations** because of how EBS snapshot lineages work:

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

## The 25% Rule (Single Threshold)

Archive tier costs exactly **25%** of Standard tier per GB, but stores the **full snapshot** (not incremental):

| Metric | Standard Tier | Archive Tier |
|--------|--------------|--------------|
| Price | $0.05/GB-month | $0.0125/GB-month |
| Storage | Incremental (unique blocks only) | Full snapshot (all blocks) |

### Break-Even Math

```
Archive saves money when:
  cost_of_unreferenced_blocks_in_standard >= cost_of_full_snapshot_in_archive

  unreferenced_blocks × $0.05 >= full_snapshot_size × $0.0125

  unreferenced_blocks >= full_snapshot_size × 25%
```

| Unreferenced % of Full Snapshot | Verdict |
|---|---|
| < 25% | ❌ Archive costs MORE (re-attribution eats savings) |
| = 25% | Break-even |
| > 25% | ✅ Archive saves money |

**No ambiguity. One number: 25%.**

## How It Measures (EBS Direct API)

Unlike CloudWatch `VolumeWriteBytes` (which overcounts due to repeated overwrites), this tool uses the **EBS Direct API** for precise measurement:

1. **`ListChangedBlocks(predecessor, target)`** — blocks that changed between the previous snapshot and the target
2. **`ListChangedBlocks(target, successor)`** — blocks that changed between the target and the next snapshot
3. **Unreferenced blocks** = blocks appearing in **both** results (unique to the target, not shared with neighbors)
4. **`ListSnapshotBlocks(target)`** — total blocks in the full snapshot

```
Unreferenced % = (unreferenced_blocks / total_blocks) × 100
```

This gives the **exact** ratio needed for the 25% comparison.

## Decision Logic

The evaluator assigns one of 7 recommendations per volume:

| # | Recommendation | Colour | Criteria |
|---|---|---|---|
| 0 | **HOUSEKEEP - EXCEEDS RETENTION** | 🟡 Yellow | Snapshots exceed user-specified max retention period |
| 1 | **HOUSEKEEP - DELETE ALL** | 🟠 Orange | All snapshots have expired retention — delete to save costs |
| 2 | **NOT ELIGIBLE** | 🔴 Red | Snapshots expire within 90 days (archive minimum retention) |
| 3 | **HOUSEKEEP FIRST** | 🟠 Orange | Mix of expired and active snapshots — clean up expired first |
| 4 | **COLD STORAGE CANDIDATE** | 🟢 Green | Volume decommissioned — no re-attribution possible |
| 5 | **COLD STORAGE CANDIDATE** | 🟢 Green | Single standalone snapshot — no lineage, saves 75% |
| 6 | **COLD STORAGE CANDIDATE** or **NOT RECOMMENDED** | 🟢/🔴 | Multiple snapshots — decided by 25% unreferenced block threshold |

### Decision Flowchart

```
┌─────────────────────────────────────┐
│ For each EBS Volume with snapshots  │
└──────────────┬──────────────────────┘
               │
               ▼
┌──────────────────────────────────┐     YES    ┌─────────────────────────────────┐
│ Max retention specified AND      │────────────▶│ HOUSEKEEP - EXCEEDS RETENTION   │
│ snapshots exceed max retention?  │             │ (flag for deletion)             │
└──────────────┬───────────────────┘             └─────────────────────────────────┘
               │ NO / not specified
               ▼
┌──────────────────────────────────┐     YES    ┌─────────────────────────┐
│ All snapshots expired?           │────────────▶│ HOUSEKEEP - DELETE ALL  │
└──────────────┬───────────────────┘             └─────────────────────────┘
               │ NO
               ▼
┌──────────────────────────────────┐     YES    ┌─────────────────────────┐
│ Any snapshot expires < 90 days?  │────────────▶│ NOT ELIGIBLE            │
└──────────────┬───────────────────┘             └─────────────────────────┘
               │ NO
               ▼
┌──────────────────────────────────┐     YES    ┌─────────────────────────┐
│ Some snapshots expired?          │────────────▶│ HOUSEKEEP FIRST         │
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
               │ NO (multiple snapshots in lineage)
               ▼
┌──────────────────────────────────┐
│ EBS Direct API:                  │
│ ListChangedBlocks +              │
│ ListSnapshotBlocks               │
│                                  │
│ unreferenced_pct =               │
│   unreferenced / full_blocks     │
└──────────────┬───────────────────┘
               │
        ┌──────┴──────┐
        │             │
        ▼             ▼
    >= 25%          < 25%
        │             │
        ▼             ▼
┌────────────┐  ┌───────────────┐
│ CANDIDATE  │  │NOT RECOMMENDED│
│ (saves $)  │  │(would increase│
│            │  │ costs)        │
└────────────┘  └───────────────┘
```

## When Cold Storage Makes Sense

### ✅ Recommended Scenarios
1. **Volume Decommissioned** — No new snapshots, all can be archived together, no re-attribution
2. **Single Standalone Snapshot** — No lineage sharing, archive saves 75%
3. **High Unreferenced Ratio (≥25%)** — Measured via EBS Direct API, confirms savings
4. **Compliance / End-of-Project Snapshots** — One-off snapshots, standalone full copies

### ❌ Not Recommended Scenarios
1. **Low Unreferenced Ratio (<25%)** — Re-attribution means warm cost stays the same + cold cost added
2. **Snapshots Expiring Within 90 Days** — Archive minimum retention makes it pointless
3. **Active Backup Lineages with High Sharing** — Most blocks are shared between neighbors

## How to Use

### Prerequisites
- Python 3.9+
- AWS credentials configured (profile or environment variables)
- IAM permissions: `ec2:DescribeSnapshots`, `ec2:DescribeVolumes`, `ebs:ListChangedBlocks`, `ebs:ListSnapshotBlocks`, `backup:ListBackupVaults`, `backup:ListRecoveryPointsByBackupVault`, `pricing:GetProducts`, `ssm:GetParameter`

### Installation

```bash
git clone https://github.com/sharmaankush123/ebs-cold-storage-evaluator.git
cd ebs-cold-storage-evaluator
pip3 install -r requirements.txt
```

### Execution

```bash
# Basic usage
python3 ebs_cold_storage_evaluator.py <aws-profile> <region>

# With max retention period (flags snapshots older than N days as housekeep candidates)
python3 ebs_cold_storage_evaluator.py <aws-profile> <region> <max-retention-days>

# Examples
python3 ebs_cold_storage_evaluator.py default us-east-1
python3 ebs_cold_storage_evaluator.py production eu-west-1
python3 ebs_cold_storage_evaluator.py default us-east-1 365   # flag snapshots older than 1 year
```

### Output

The script generates:
1. **CSV file** — machine-readable results
2. **Excel file (.xlsx)** — colour-coded recommendations with two sheets:
   - **Cold Storage Assessment** — per-volume recommendations (colour-coded)
   - **Cost Savings Summary** — total potential savings if recommendations are applied

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
| `newest_full_snapshot_gb` | Full snapshot size (what archive tier would store) |
| `unreferenced_pct` | % of blocks unique to this snapshot (the key metric) |
| `expired_snapshots` | Count of snapshots past their retention date |
| `exceeds_retention` | Count of snapshots exceeding max retention period (if specified) |
| `min_days_to_expiry` | Shortest time until a snapshot expires |
| `warm_cost_per_month_est` | Estimated monthly cost in Standard tier |
| `cold_cost_per_month_est` | Estimated monthly cost in Archive tier |
| `recommendation` | Volume-level recommendation |
| `decision_reason` | Detailed explanation |

### Cost Savings Summary Sheet

The second Excel sheet provides an at-a-glance savings breakdown:

| Category | Description |
|----------|-------------|
| Archive to Cold Storage | Savings from transitioning candidates to archive tier |
| Housekeep (Delete Expired) | Savings from deleting expired snapshots |
| Exceeds Retention (Delete) | Savings from deleting over-retained snapshots |
| **Total Potential Savings** | Combined monthly and annual savings |

## Cost Comparison Example

**1 TB volume, 2 snapshots in lineage:**

| Unreferenced % | Standard Cost (unreferenced blocks) | Archive Cost (full snapshot) | Verdict |
|---|---|---|---|
| 10% (~100 GB unique) | $5.00/month | $12.80/month (1 TB full) | ❌ Archive costs 2.5× more |
| 25% (~256 GB unique) | $12.80/month | $12.80/month | Break-even |
| 50% (~512 GB unique) | $25.60/month | $12.80/month | ✅ Archive saves 50% |
| 100% (standalone) | $51.20/month | $12.80/month | ✅ Archive saves 75% |

## Assumptions & Notes
- Archive tier has a **minimum 90-day retention** — snapshots deleted before 90 days incur pro-rated charges
- Archive tier restoration takes **up to 72 hours**
- `ListChangedBlocks` and `ListSnapshotBlocks` APIs have associated costs — see [EBS Pricing](https://aws.amazon.com/ebs/pricing/)
- The script evaluates the **newest snapshot** in each lineage as the archive candidate
- For decommissioned volumes, all snapshots can be archived together (no re-attribution)

## References
- [AWS: Archive Amazon EBS Snapshots](https://docs.aws.amazon.com/ebs/latest/userguide/snapshot-archive.html)
- [AWS: Guidelines and Best Practices for Archiving](https://docs.aws.amazon.com/ebs/latest/userguide/archiving-guidelines.html)
- [AWS: Determining the Reduction in Standard Tier Storage Costs](https://docs.aws.amazon.com/ebs/latest/userguide/archiving-guidelines.html#archive-guidelines)
- [AWS Blog: New Amazon EBS Snapshots Archive](https://aws.amazon.com/blogs/aws/new-amazon-ebs-snapshots-archive/)
- [EBS Pricing](https://aws.amazon.com/ebs/pricing/)

## Security

See [CONTRIBUTING](CONTRIBUTING.md) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
