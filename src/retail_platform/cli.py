"""Command-line entry point: ``retail <command>``.

Exit codes: 0 success, 1 failure, 2 partial run (some batches rejected or a source down),
3 another run is already in progress.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date

from retail_platform.config import (
    SimulationSettings,
    get_settings,
    get_simulation_settings,
)
from retail_platform.errors import PipelineAlreadyRunningError
from retail_platform.logging_setup import configure_logging

logger = logging.getLogger("retail_platform.cli")

EXIT_OK, EXIT_FAILED, EXIT_PARTIAL, EXIT_LOCKED = 0, 1, 2, 3


def _cmd_migrate(_: argparse.Namespace) -> int:
    from retail_platform.warehouse.db import make_engine, migrate

    applied = migrate(make_engine(get_settings().database_url))
    logger.info("migrations up to date", extra={"applied": applied})
    return EXIT_OK


def _cmd_generate(args: argparse.Namespace) -> int:
    from retail_platform.synthetic.simulation import simulate
    from retail_platform.synthetic.writer import write_landing

    base = get_simulation_settings()
    sim = SimulationSettings(
        seed=args.seed if args.seed is not None else base.seed,
        start_date=args.start_date or base.start_date,
        days=args.days or base.days,
        traffic_scale=args.traffic_scale or base.traffic_scale,
        dirty_rate=args.dirty_rate if args.dirty_rate is not None else base.dirty_rate,
    )
    world = simulate(sim.seed, sim.start_date, sim.days, sim.traffic_scale)
    summary = write_landing(world, get_settings().landing_dir, sim.seed, sim.dirty_rate)
    print(
        f"{summary.sales_files_written} sales files written, "
        f"{summary.sales_files_unchanged} unchanged, {summary.sales_rows} rows, "
        f"injected defects: {summary.defects}"
    )
    return EXIT_OK


def _run_once(args: argparse.Namespace) -> int:
    from retail_platform.pipeline.runner import ALL_SOURCES, Pipeline
    from retail_platform.warehouse.db import make_engine

    settings = get_settings()
    pipeline = Pipeline(make_engine(settings.database_url), settings)
    sources = tuple(args.only) if args.only else ALL_SOURCES
    try:
        result = pipeline.run(full_refresh=args.full_refresh, sources=sources, trigger=args.trigger)
    except PipelineAlreadyRunningError as exc:
        logger.warning(str(exc))
        return EXIT_LOCKED
    return {"success": EXIT_OK, "partial": EXIT_PARTIAL}.get(result.status, EXIT_FAILED)


def _cmd_run(args: argparse.Namespace) -> int:
    return _run_once(args)


def _cmd_schedule(args: argparse.Namespace) -> int:
    """Tiny built-in scheduler for the Docker demo (use cron/Airflow in production)."""
    logger.info("scheduler started", extra={"interval_seconds": args.interval})
    while True:
        try:
            _run_once(args)
        except Exception:
            logger.exception("scheduled run crashed; will retry on next tick")
        time.sleep(args.interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="retail", description="Retail data platform CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=_cmd_migrate)

    gen = sub.add_parser("generate", help="write synthetic CSVs to the landing zone")
    gen.add_argument("--seed", type=int)
    gen.add_argument("--start-date", type=date.fromisoformat)
    gen.add_argument("--days", type=int)
    gen.add_argument("--traffic-scale", type=float)
    gen.add_argument("--dirty-rate", type=float)
    gen.set_defaults(func=_cmd_generate)

    for name, func, helptext in (
        ("run", _cmd_run, "run the pipeline once"),
        ("schedule", _cmd_schedule, "run the pipeline every --interval seconds"),
    ):
        cmd = sub.add_parser(name, help=helptext)
        cmd.add_argument("--full-refresh", action="store_true", help="ignore registry/watermarks")
        cmd.add_argument(
            "--only", nargs="+", choices=["master", "sales", "inventory"], help="subset of sources"
        )
        cmd.add_argument("--trigger", default="cli" if name == "run" else "schedule")
        if name == "schedule":
            cmd.add_argument("--interval", type=int, default=3600)
        cmd.set_defaults(func=func)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    try:
        code: int = args.func(args)
    except Exception:
        logger.exception("command failed", extra={"command": args.command})
        return EXIT_FAILED
    return code


if __name__ == "__main__":
    sys.exit(main())
