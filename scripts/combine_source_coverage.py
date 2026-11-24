"""Combine coverage from every shard of one verified complete-source plan."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from coverage import Coverage, CoverageData
from coverage.exceptions import CoverageException
from loguru import logger

from auto_coder.ci_shard_plan import read_plan
from auto_coder.source_ci_evidence import _artifact_name

LOG_FORMAT = "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level}</level> | {file.name}:{function}:{line} | {message}"


def combine(plan_path: Path, artifacts: Path, output: Path) -> float:
    """Require each planned shard's measured data before creating any report."""
    plan, _raw = read_plan(plan_path)
    run_id = int(str(plan.context.origin["run_id"]))
    attempt = int(str(plan.context.origin["run_attempt"]))
    coverage = Coverage(data_file=str(output / "coverage.db"), source=["src/auto_coder"], config_file=False)
    combined = coverage.get_data()
    for shard in range(1, len(plan.shards) + 1):
        path = artifacts / _artifact_name(run_id, attempt, shard) / "coverage" / "coverage.db"
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"coverage is missing for planned source shard {shard}")
        data = CoverageData(basename=str(path))
        data.read()
        if not data.measured_files():
            raise ValueError(f"coverage is empty for planned source shard {shard}")
        combined.update(data)
        logger.info("Included source coverage for run {} attempt {} shard {}/{}", run_id, attempt, shard, len(plan.shards))
    output.mkdir(parents=True, exist_ok=True)
    coverage.save()
    coverage.html_report(directory=str(output / "htmlcov"))
    coverage.xml_report(outfile=str(output / "coverage.xml"))
    with (output / "summary.txt").open("w", encoding="utf-8") as stream:
        percent = coverage.report(file=stream)
    logger.info("Combined coverage for tested source {}: {:.2f}%", plan.context.checkout_sha, percent)
    return percent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    logger.remove()
    logger.add(sys.stdout, format=LOG_FORMAT, colorize=True)
    logger.add(args.output / "coverage-aggregation.log", format=LOG_FORMAT, rotation="10 MB", retention=5)
    try:
        combine(args.plan, args.artifacts, args.output)
    except (CoverageException, OSError, ValueError, KeyError) as error:
        logger.error("Complete source coverage refused: {}", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
