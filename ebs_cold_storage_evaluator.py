#!/usr/bin/env python3
"""
EBS Snapshot Tier Cost Evaluator v2
Evaluates cold storage eligibility at the VOLUME level (not individual snapshots).

Key insight: Archiving individual snapshots from a lineage does NOT save money because
referenced blocks get re-attributed to remaining warm snapshots. Cold storage only
makes sense when evaluated holistically per volume.

Cold storage is viable when:
1. Volume is decommissioned (no new snapshots, all can be archived together)
2. Standalone/only snapshot (no lineage sharing)
3. High-churn volume (>20% monthly change rate makes monthly snapshots cost-effective to archive)
4. Compliance-only retention (snapshots kept for audit, rarely accessed)
"""

import boto3
import json
import csv
import sys
from datetime import datetime, timedelta

try:
    from openpyxl import Workbook
    from openpyxl.styles import PatternFill, Font, Alignment
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

BLOCK_SIZE_BYTES = 524288  # 512 KiB
ARCHIVE_MIN_DAYS = 90


def get_ebs_pricing(session, region):
    """Get EBS snapshot pricing for Standard and Archive tiers."""
    ssm = session.client("ssm", region_name="us-east-1")
    try:
        param = ssm.get_parameter(Name=f"/aws/service/global-infrastructure/regions/{region}/longName")
        location = param["Parameter"]["Value"]
    except Exception:
        location = region

    pricing_client = session.client("pricing", region_name="us-east-1")
    try:
        std_response = pricing_client.get_products(
            ServiceCode="AmazonEC2",
            Filters=[
                {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage Snapshot"},
                {"Type": "TERM_MATCH", "Field": "storageMedia", "Value": "Amazon S3"},
                {"Type": "TERM_MATCH", "Field": "location", "Value": location},
            ],
            MaxResults=10,
        )
        std_price = 0.05
        for item in std_response["PriceList"]:
            product = json.loads(item)
            terms = product.get("terms", {}).get("OnDemand", {})
            for term in terms.values():
                for dim in term.get("priceDimensions", {}).values():
                    price = float(dim["pricePerUnit"]["USD"])
                    if price > 0:
                        std_price = price
                        break
    except Exception:
        std_price = 0.05
    return std_price, 0.0125


def get_snapshots(ec2_client):
    """Get all completed standard-tier snapshots owned by this account."""
    snapshots = []
    paginator = ec2_client.get_paginator("describe_snapshots")
    for page in paginator.paginate(Filters=[
        {"Name": "status", "Values": ["completed"]},
        {"Name": "storage-tier", "Values": ["standard"]},
    ], OwnerIds=["self"]):
        snapshots.extend(page["Snapshots"])
    return snapshots


def get_backup_expiration(session):
    """Get expiration info from AWS Backup recovery points."""
    backup_client = session.client("backup")
    snap_expiration = {}
    try:
        vaults = backup_client.list_backup_vaults().get("BackupVaultList", [])
        for vault in vaults:
            paginator = backup_client.get_paginator("list_recovery_points_by_backup_vault")
            for page in paginator.paginate(BackupVaultName=vault["BackupVaultName"]):
                for rp in page.get("RecoveryPoints", []):
                    if rp.get("ResourceType") != "EBS":
                        continue
                    rp_arn = rp.get("RecoveryPointArn", "")
                    if "/snap-" not in rp_arn:
                        continue
                    snap_id = rp_arn.split("/")[-1]
                    delete_at = rp.get("CalculatedLifecycle", {}).get("DeleteAt")
                    lifecycle_days = rp.get("Lifecycle", {}).get("DeleteAfterDays")
                    if delete_at:
                        snap_expiration[snap_id] = {
                            "delete_at": delete_at,
                            "days_left": (delete_at - datetime.now(delete_at.tzinfo)).days,
                            "retention_days": lifecycle_days,
                            "source": f"AWS Backup ({vault['BackupVaultName']})",
                        }
    except Exception as e:
        print(f"    Warning: Could not fully query AWS Backup: {e}")
    return snap_expiration


def check_volume_exists(ec2_client, volume_id):
    """Check if the source volume still exists."""
    if volume_id in ("vol-ffffffff", "unknown"):
        return False
    try:
        ec2_client.describe_volumes(VolumeIds=[volume_id])
        return True
    except Exception:
        return False


def get_volume_write_activity(cw_client, volume_id, days=30):
    """Get VolumeWriteBytes from CloudWatch to estimate change rate."""
    try:
        end = datetime.utcnow()
        start = end - timedelta(days=days)
        resp = cw_client.get_metric_statistics(
            Namespace="AWS/EBS",
            MetricName="VolumeWriteBytes",
            Dimensions=[{"Name": "VolumeId", "Value": volume_id}],
            StartTime=start, EndTime=end,
            Period=86400 * days,  # single datapoint for entire period
            Statistics=["Sum"],
        )
        datapoints = resp.get("Datapoints", [])
        if datapoints:
            return datapoints[0]["Sum"]
    except Exception:
        pass
    return None


def evaluate_volumes(region, profile=None):
    """Main evaluation - at VOLUME level."""
    session = boto3.Session(profile_name=profile, region_name=region)
    ec2 = session.client("ec2")
    cw = session.client("cloudwatch")

    print(f"\n{'='*70}")
    print(f"EBS Snapshot Cold Storage Evaluator v2 (Volume-Level Analysis)")
    print(f"Region: {region} | Profile: {profile or 'default'}")
    print(f"{'='*70}")

    # Pricing
    print("\n[1/5] Fetching EBS pricing...")
    std_price, archive_price = get_ebs_pricing(session, region)
    print(f"  Standard: ${std_price}/GB-month | Archive: ${archive_price}/GB-month")

    # Snapshots
    print("\n[2/5] Discovering snapshots...")
    snapshots = get_snapshots(ec2)
    print(f"  Found {len(snapshots)} completed standard-tier snapshots")
    if not snapshots:
        print("  No snapshots found. Exiting.")
        return

    # Expiration
    print("\n[3/5] Checking expiration dates...")
    snap_expiration = get_backup_expiration(session)
    print(f"  Found expiration info for {len(snap_expiration)} snapshots")

    # Group by volume
    print("\n[4/5] Grouping by volume...")
    vol_snaps = {}
    for snap in snapshots:
        vol_id = snap.get("VolumeId", "unknown")
        vol_snaps.setdefault(vol_id, []).append(snap)
    for vol_id in vol_snaps:
        vol_snaps[vol_id].sort(key=lambda s: s["StartTime"])
    print(f"  {len(vol_snaps)} unique volumes")

    # Evaluate each volume
    print("\n[5/5] Evaluating volumes for cold storage eligibility...")
    volume_results = []

    for vol_id, snaps in vol_snaps.items():
        print(f"\n  Volume: {vol_id} ({len(snaps)} snapshots)")

        vol_size_gb = snaps[0]["VolumeSize"]
        oldest_snap = snaps[0]
        newest_snap = snaps[-1]
        oldest_date = oldest_snap["StartTime"]
        newest_date = newest_snap["StartTime"]
        span_days = (newest_date - oldest_date).days

        # Check if volume still exists (decommissioned = strong archive candidate)
        volume_exists = check_volume_exists(ec2, vol_id)

        # Check expiration status for all snapshots
        expired_count = 0
        active_expiry_count = 0
        min_expiry_days = None
        max_retention = None
        for s in snaps:
            sid = s["SnapshotId"]
            if sid in snap_expiration:
                days_left = snap_expiration[sid]["days_left"]
                retention = snap_expiration[sid].get("retention_days")
                if retention:
                    max_retention = max(max_retention or 0, retention)
                if days_left < 0:
                    expired_count += 1
                else:
                    active_expiry_count += 1
                    if min_expiry_days is None or days_left < min_expiry_days:
                        min_expiry_days = days_left

        # Estimate total warm storage (sum of full snapshot sizes as proxy)
        total_full_size_gb = sum(int(s.get("FullSnapshotSizeInBytes", 0)) for s in snaps) / (1024**3)
        # For warm tier, actual billed = incremental (much less than sum of full sizes)
        # Best proxy: newest snapshot's full size ≈ total unique data across lineage
        newest_full_gb = int(newest_snap.get("FullSnapshotSizeInBytes", 0)) / (1024**3)
        if newest_full_gb == 0:
            newest_full_gb = vol_size_gb  # fallback

        # Estimate monthly change rate from CloudWatch (if volume exists)
        monthly_change_rate = None
        write_bytes_30d = None
        if volume_exists:
            write_bytes_30d = get_volume_write_activity(cw, vol_id, days=30)
            if write_bytes_30d and vol_size_gb > 0:
                write_gb = write_bytes_30d / (1024**3)
                monthly_change_rate = (write_gb / vol_size_gb) * 100  # percentage

        # === COLD STORAGE ELIGIBILITY DECISION ===
        cold_eligible = False
        recommendation = ""
        reason = ""

        # Case 1: All snapshots expired - HOUSEKEEP first
        if expired_count == len(snaps):
            recommendation = "HOUSEKEEP - DELETE ALL"
            reason = (f"All {len(snaps)} snapshots have expired retention. "
                      f"Delete to save ~${newest_full_gb * std_price * 3:.2f}/90d. "
                      f"No cold storage needed - just delete.")

        # Case 2: Volume decommissioned (doesn't exist) + no active expiry
        elif not volume_exists and active_expiry_count == 0:
            cold_eligible = True
            recommendation = "COLD STORAGE CANDIDATE"
            reason = (f"Volume decommissioned (no longer exists). No new snapshots being created. "
                      f"All {len(snaps)} snapshots can be archived together as standalone full snapshots. "
                      f"No block re-attribution issue since no warm snapshots will remain.")

        # Case 3: Volume decommissioned but has active expiry < 90 days
        elif not volume_exists and min_expiry_days is not None and min_expiry_days < ARCHIVE_MIN_DAYS:
            recommendation = "NOT ELIGIBLE"
            reason = (f"Volume decommissioned but snapshots expire in {min_expiry_days} days "
                      f"(< 90 day archive minimum). Let retention policy delete them naturally.")

        # Case 4: Single snapshot (no lineage)
        elif len(snaps) == 1:
            if newest_full_gb * archive_price < newest_full_gb * std_price:
                cold_eligible = True
                recommendation = "COLD STORAGE CANDIDATE"
                reason = (f"Single standalone snapshot. No lineage sharing. "
                          f"Archive saves 75% (${newest_full_gb * std_price:.2f} → "
                          f"${newest_full_gb * archive_price:.2f}/month).")
            else:
                recommendation = "KEEP IN STANDARD"
                reason = "Single snapshot but archive not cheaper (unexpected - check pricing)."

        # Case 5: Volume exists with high change rate (>20%)
        elif volume_exists and monthly_change_rate is not None and monthly_change_rate > 20:
            cold_eligible = True
            recommendation = "COLD STORAGE CANDIDATE (HIGH CHURN)"
            reason = (f"Volume has {monthly_change_rate:.1f}% monthly change rate (>{20}% threshold). "
                      f"Monthly snapshots accumulate unique blocks quickly, making cold storage "
                      f"cost-effective for long-retention monthly snapshots. "
                      f"NOTE: Only archive monthly snapshots, keep daily/weekly in standard.")

        # Case 6: Volume exists with moderate change rate (15-20%)
        elif volume_exists and monthly_change_rate is not None and 15 <= monthly_change_rate <= 20:
            recommendation = "BORDERLINE - REVIEW"
            reason = (f"Volume has {monthly_change_rate:.1f}% monthly change rate (15-20% borderline zone). "
                      f"Cold storage may break even. Recommend monitoring for 2-3 months before deciding. "
                      f"Consider only if retention > 12 months.")

        # Case 7: Volume exists with low change rate (<15%)
        elif volume_exists and monthly_change_rate is not None and monthly_change_rate < 15:
            recommendation = "NOT RECOMMENDED"
            reason = (f"Volume has {monthly_change_rate:.1f}% monthly change rate (<15% threshold). "
                      f"Archiving would convert incremental to full snapshots, likely INCREASING costs. "
                      f"Block re-attribution means warm tier cost stays the same + cold cost added.")

        # Case 8: Volume exists but no CloudWatch data
        elif volume_exists and monthly_change_rate is None:
            recommendation = "INSUFFICIENT DATA"
            reason = (f"Volume exists but no CloudWatch VolumeWriteBytes data available. "
                      f"Enable detailed monitoring or check if volume is attached. "
                      f"Cannot determine change rate for cold storage assessment.")

        # Case 9: Mixed expired + active
        elif expired_count > 0:
            recommendation = "HOUSEKEEP FIRST"
            reason = (f"{expired_count}/{len(snaps)} snapshots have expired retention. "
                      f"Delete expired snapshots first, then re-evaluate remaining for cold storage.")

        # Default
        else:
            recommendation = "KEEP IN STANDARD"
            reason = "No clear benefit from cold storage transition for this volume's snapshot lineage."

        volume_results.append({
            "region": region,
            "volume_id": vol_id,
            "volume_size_gb": vol_size_gb,
            "volume_exists": "Yes" if volume_exists else "No (Decommissioned)",
            "total_snapshots": len(snaps),
            "oldest_snapshot_id": oldest_snap["SnapshotId"],
            "oldest_snapshot_date": oldest_date.isoformat(),
            "newest_snapshot_id": newest_snap["SnapshotId"],
            "newest_snapshot_date": newest_date.isoformat(),
            "snapshot_span_days": span_days,
            "monthly_change_rate_pct": f"{monthly_change_rate:.1f}%" if monthly_change_rate is not None else "N/A",
            "write_bytes_30d_gb": round(write_bytes_30d / (1024**3), 2) if write_bytes_30d else "N/A",
            "newest_full_snapshot_gb": round(newest_full_gb, 2),
            "expired_snapshots": expired_count,
            "active_expiry_snapshots": active_expiry_count,
            "min_days_to_expiry": min_expiry_days if min_expiry_days is not None else "No expiry",
            "max_retention_days": max_retention if max_retention else "N/A",
            "warm_cost_per_month_est": round(newest_full_gb * std_price, 2),
            "cold_cost_per_month_est": round(newest_full_gb * archive_price, 2),
            "recommendation": recommendation,
            "decision_reason": reason,
        })

        print(f"    → {recommendation}")

    # Output
    print(f"\nWriting results...")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_filename = f"ebs_cold_storage_eval_{region}_{timestamp}.csv"

    if volume_results:
        with open(csv_filename, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=volume_results[0].keys())
            writer.writeheader()
            writer.writerows(volume_results)
        print(f"  CSV: {csv_filename}")

        if HAS_OPENPYXL:
            xlsx_filename = f"ebs_cold_storage_eval_{region}_{timestamp}.xlsx"
            wb = Workbook()

            # Sheet 1: Volume-Level Cold Storage Assessment
            ws = wb.active
            ws.title = "Cold Storage Assessment"
            headers = list(volume_results[0].keys())
            ws.append(headers)

            green_fill = PatternFill(start_color="90EE90", end_color="90EE90", fill_type="solid")
            orange_fill = PatternFill(start_color="FFB347", end_color="FFB347", fill_type="solid")
            red_fill = PatternFill(start_color="FF6B6B", end_color="FF6B6B", fill_type="solid")
            yellow_fill = PatternFill(start_color="FFFF99", end_color="FFFF99", fill_type="solid")

            for row_data in volume_results:
                ws.append([row_data[h] for h in headers])
                rec = row_data["recommendation"]
                if "COLD STORAGE CANDIDATE" in rec:
                    for cell in ws[ws.max_row]:
                        cell.fill = green_fill
                elif "HOUSEKEEP" in rec:
                    for cell in ws[ws.max_row]:
                        cell.fill = orange_fill
                elif "NOT RECOMMENDED" in rec or "NOT ELIGIBLE" in rec:
                    for cell in ws[ws.max_row]:
                        cell.fill = red_fill
                elif "BORDERLINE" in rec:
                    for cell in ws[ws.max_row]:
                        cell.fill = yellow_fill

            # Sheet 2: Snapshot Detail (for reference)
            ws2 = wb.create_sheet("Snapshot Detail")
            snap_headers = ["region", "volume_id", "snapshot_id", "created", "volume_size_gb",
                            "full_snapshot_size_gb", "expiry_status", "expiry_source"]
            ws2.append(snap_headers)
            for vol_id, snaps in vol_snaps.items():
                for s in snaps:
                    sid = s["SnapshotId"]
                    full_gb = round(int(s.get("FullSnapshotSizeInBytes", 0)) / (1024**3), 2)
                    exp_info = snap_expiration.get(sid, {})
                    days_left = exp_info.get("days_left")
                    if days_left is not None:
                        expiry_status = f"Expired ({abs(days_left)}d ago)" if days_left < 0 else f"{days_left}d remaining"
                    else:
                        expiry_status = "No expiry set"
                    ws2.append([region, vol_id, sid, s["StartTime"].isoformat(),
                                s["VolumeSize"], full_gb, expiry_status,
                                exp_info.get("source", "N/A")])

            wb.save(xlsx_filename)
            print(f"  Excel: {xlsx_filename}")
            print(f"    Sheet 1: Cold Storage Assessment (volume-level)")
            print(f"    Sheet 2: Snapshot Detail (individual snapshots)")

    # Summary
    print(f"\n{'='*70}")
    print(f"SUMMARY - {region}")
    print(f"{'='*70}")
    print(f"  Volumes evaluated: {len(volume_results)}")
    cold_candidates = [r for r in volume_results if "COLD STORAGE CANDIDATE" in r["recommendation"]]
    housekeep = [r for r in volume_results if "HOUSEKEEP" in r["recommendation"]]
    not_recommended = [r for r in volume_results if "NOT RECOMMENDED" in r["recommendation"]]
    borderline = [r for r in volume_results if "BORDERLINE" in r["recommendation"]]
    print(f"  Cold storage candidates:    {len(cold_candidates)} (green)")
    print(f"  Housekeep/delete:           {len(housekeep)} (orange)")
    print(f"  Not recommended for cold:   {len(not_recommended)} (red)")
    print(f"  Borderline - review:        {len(borderline)} (yellow)")
    if cold_candidates:
        print(f"\n  COLD STORAGE CANDIDATES:")
        for c in cold_candidates:
            print(f"    {c['volume_id']} | {c['total_snapshots']} snaps | "
                  f"{c['monthly_change_rate_pct']} change | {c['recommendation']}")
    print(f"{'='*70}\n")

    return volume_results


if __name__ == "__main__":
    profile = sys.argv[1] if len(sys.argv) > 1 else None
    region = sys.argv[2] if len(sys.argv) > 2 else "us-east-1"
    evaluate_volumes(region=region, profile=profile)
