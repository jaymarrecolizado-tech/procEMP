"""Synthetic anonymized dataset plan + generators (Track A scaffolding).

Produces ONLY synthetic, non-confidential samples for plumbing checks,
fine-tuning format validation, and benchmark-slice design. No real bid
data is used, and no real names/identifiers are embedded. Real training
requires approved/anonymized historical samples with DPO/BAC sign-off
(see procurement-review/05-data-governance-training.md); this module
explicitly does NOT claim that approval.

Slices (benchmark + training coverage):
- clean_digital: well-formed digital text (baseline)
- taglish_mixed: Taglish / mixed-language phrasing common in scanned admin docs
- sparse_scan: short / noisy / partial text (tests ABSTAIN behavior)
- unreadable: document level UNREADABLE (tests never-disqualify rule)
- missing_field: a required field absent from text (tests ABSTAIN/FLAG)

Usage:
  python dataset.py samples --out samples.jsonl
  python dataset.py benchmark --out benchmark.json
  python dataset.py sft --out sft.jsonl
"""

from __future__ import annotations

import argparse
import json
import os

from core import canonical, now, sha256_text
from extraction import load_schema, validate_output
from training_adapter import build_sft_record, write_jsonl

DATASET_PLAN_VERSION = "1.0-synthetic"


def _sample_cases():
    """Return synthetic cases: {name, slice_name, pages, target}."""
    base_target = {
        "model_version": "synthetic-seed-v1",
        "documents": {
            "philgeps_registration": {
                "status": "OK",
                "fields": {"present": True, "valid_until": "2027-06-30"},
                "confidence": {"present": 0.98, "valid_until": 0.95},
                "evidence": {"valid_until": {"page": 1, "snippet": "valid until 2027-06-30"}},
            },
            "omnibus_sworn_statement": {
                "status": "OK",
                "fields": {"present": True, "signed": True, "notarized": True},
                "confidence": {"present": 0.97, "signed": 0.95, "notarized": 0.9},
                "evidence": {},
            },
            "bid_securing_declaration": {
                "status": "OK",
                "fields": {"present": True, "signed": True},
                "confidence": {"present": 0.97, "signed": 0.95},
                "evidence": {},
            },
            "financial_proposal": {
                "status": "OK",
                "fields": {"present": True, "amount": 950000},
                "confidence": {"present": 0.98, "amount": 0.96},
                "evidence": {"amount": {"page": 2, "snippet": "PHP 950,000.00"}},
            },
        },
    }

    def clone(target):
        return json.loads(json.dumps(target))

    cases = []

    cases.append({
        "name": "clean_digital_01",
        "slice_name": "clean_digital",
        "pages": [
            {"page": 1, "text": "PhilGEPS Platinum Registration, valid until 2027-06-30"},
            {"page": 2, "text": "Financial proposal: PHP 950,000.00"},
            {"page": 3, "text": "Omnibus Sworn Statement, signed and notarized"},
            {"page": 4, "text": "Bid Securing Declaration, signed"},
        ],
        "target": clone(base_target),
    })

    taglish = clone(base_target)
    cases.append({
        "name": "taglish_01",
        "slice_name": "taglish_mixed",
        "pages": [
            {"page": 1, "text": "PhilGEPS Platinum, hanggang 2027-06-30 ang validity"},
            {"page": 2, "text": "Presyong iniaalok: PHP 950,000.00 lamang"},
            {"page": 3, "text": "Pinirmahan at naka-notaryo ang sinumpaang salaysay"},
        ],
        "target": taglish,
    })

    sparse = clone(base_target)
    # Sparse scan: omit notarized evidence; lower confidence on one field to
    # exercise uncertain-field handling (validator will omit it; target as-is
    # is still schema-valid but checker would ABSTAIN on missing field).
    sparse["documents"]["omnibus_sworn_statement"]["confidence"]["notarized"] = 0.5
    cases.append({
        "name": "sparse_scan_01",
        "slice_name": "sparse_scan",
        "pages": [{"page": 1, "text": "PhilGEPS Platinum registered."}],
        "target": sparse,
    })

    unreadable = clone(base_target)
    unreadable["documents"]["philgeps_registration"] = {"status": "UNREADABLE"}
    cases.append({
        "name": "unreadable_01",
        "slice_name": "unreadable",
        "pages": [
            {"page": 1, "text": "[illegible scan - page unreadable]"},
            {"page": 2, "text": "Financial proposal: PHP 950,000.00"},
        ],
        "target": unreadable,
    })

    missing = clone(base_target)
    del missing["documents"]["financial_proposal"]["fields"]["amount"]
    missing["documents"]["financial_proposal"]["confidence"] = {"present": 0.98}
    cases.append({
        "name": "missing_field_01",
        "slice_name": "missing_field",
        "pages": [
            {"page": 1, "text": "PhilGEPS Platinum, valid until 2027-06-30"},
            {"page": 2, "text": "Financial proposal enclosed (amount not legible)"},
        ],
        "target": missing,
    })

    return cases


def build_benchmark_cases(frozen_by, source_ref_prefix="synthetic"):
    """Benchmark cases in learning.register_benchmark_cases shape."""
    out = []
    for case in _sample_cases():
        out.append({
            "case_id": f"{source_ref_prefix}-{case['name']}",
            "slice_name": case["slice_name"],
            "input": {"pages": case["pages"]},
            "expected": case["target"],
            "source_ref": f"{source_ref_prefix}:{case['name']} (synthetic, non-confidential)",
        })
    return out


def build_sft_records(schema=None):
    schema = schema or load_schema()
    records = []
    for case in _sample_cases():
        validation = validate_output(case["target"], schema, case["pages"])
        records.append(build_sft_record(
            pages=case["pages"],
            target_response=case["target"],
            schema=schema,
            metadata={
                "source": "synthetic_dataset_plan",
                "slice_name": case["slice_name"],
                "case_name": case["name"],
                "dataset_plan_version": DATASET_PLAN_VERSION,
                "synthetic": True,
                "approved_by": "NOT-APPROVED; synthetic only",
                "target_schema_valid": validation.valid,
                "generated_at": now(),
            },
        ))
    return records


def provenance_summary():
    cases = _sample_cases()
    slices = sorted({c["slice_name"] for c in cases})
    return {
        "dataset_plan_version": DATASET_PLAN_VERSION,
        "generated_at": now(),
        "synthetic_only": True,
        "confidential_data_used": False,
        "real_training_approved": False,
        "approval_note": "Synthetic plumbing/format validation only. Real training "
                         "requires anonymized/approved samples + DPO/BAC sign-off.",
        "cases": len(cases),
        "slices": slices,
        "schema_version": load_schema().get("schema_version"),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Synthetic dataset plan generator (no confidential data)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("samples", help="Write sample cases JSON")
    p.add_argument("--out", required=True)
    p = sub.add_parser("benchmark", help="Write frozen benchmark cases JSON (for register_benchmark_cases)")
    p.add_argument("--out", required=True)
    p = sub.add_parser("sft", help="Write SFT JSONL records")
    p.add_argument("--out", required=True)
    p = sub.add_parser("provenance", help="Print provenance summary")
    args = parser.parse_args(argv)

    if args.cmd == "samples":
        data = {"cases": [
            {"name": c["name"], "slice_name": c["slice_name"], "pages": c["pages"],
             "target": c["target"]}
            for c in _sample_cases()],
            "provenance": provenance_summary(),
        }
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        print(canonical({"wrote": args.out, "cases": len(data["cases"])}))
        return 0
    if args.cmd == "benchmark":
        cases = build_benchmark_cases(frozen_by="(set at freeze time)")
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(cases, fh, indent=2, ensure_ascii=False)
        print(canonical({"wrote": args.out, "benchmark_cases": len(cases)}))
        return 0
    if args.cmd == "sft":
        records = build_sft_records()
        write_jsonl(records, args.out)
        print(canonical({"wrote": args.out, "sft_records": len(records)}))
        return 0
    if args.cmd == "provenance":
        print(json.dumps(provenance_summary(), indent=2, ensure_ascii=False))
        return 0
    raise SystemExit(f"unknown command: {args.cmd}")


if __name__ == "__main__":
    raise SystemExit(main())
