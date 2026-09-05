"""离线评测 CLI。

用法：
    python -m scripts.evaluate_personalization build --days 90
    python -m scripts.evaluate_personalization generate --target memory_renderer --dataset dataset-xxx
    python -m scripts.evaluate_personalization evaluate --candidate pcand-xxx --dataset dataset-xxx
    python -m scripts.evaluate_personalization gates --candidate pcand-xxx
    python -m scripts.evaluate_personalization publish --candidate pcand-xxx --status approved --approver admin
"""

from __future__ import annotations

import argparse
import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("evaluate_personalization")


async def cmd_build(args):
    from agent.evolution import DatasetBuilder
    from config import get_settings
    from db.mongo import MongoDB

    settings = get_settings()
    await MongoDB.connect(
        uri=settings.MONGODB_URI,
        db_name=settings.MONGODB_DB,
        max_pool_size=settings.MONGODB_MAX_POOL_SIZE,
        min_pool_size=settings.MONGODB_MIN_POOL_SIZE,
    )
    db = MongoDB.get_db()

    builder = DatasetBuilder(db)
    result = await builder.build(days=args.days)
    logger.info("Dataset built: %s", result)
    await MongoDB.close()


async def cmd_generate(args):
    from agent.evolution import CandidateGenerator
    from config import get_settings
    from db.mongo import MongoDB

    settings = get_settings()
    await MongoDB.connect(
        uri=settings.MONGODB_URI,
        db_name=settings.MONGODB_DB,
        max_pool_size=settings.MONGODB_MAX_POOL_SIZE,
        min_pool_size=settings.MONGODB_MIN_POOL_SIZE,
    )
    db = MongoDB.get_db()

    gen = CandidateGenerator(db)
    result = await gen.generate(
        args.target,
        args.dataset,
        baseline_snapshot_id=args.baseline,
        hypothesis=args.hypothesis,
        target_failures=args.target_failure,
        expected_metrics={args.metric: args.expected_delta},
    )
    logger.info("Candidate generated: %s", result["candidate_id"])
    await MongoDB.close()


async def cmd_evaluate(args):
    from agent.evolution import Evaluator
    from config import get_settings
    from db.mongo import MongoDB

    settings = get_settings()
    await MongoDB.connect(
        uri=settings.MONGODB_URI,
        db_name=settings.MONGODB_DB,
        max_pool_size=settings.MONGODB_MAX_POOL_SIZE,
        min_pool_size=settings.MONGODB_MIN_POOL_SIZE,
    )
    db = MongoDB.get_db()

    evaluator = Evaluator(db)
    result = await evaluator.evaluate(args.candidate, args.dataset)
    logger.info(
        "Evaluation result: fitness=%s, holdout=%s",
        result.get("fitness"),
        result.get("holdout_fitness"),
    )
    await MongoDB.close()


async def cmd_gates(args):
    from agent.evolution import GateChecker
    from config import get_settings
    from db.mongo import MongoDB

    settings = get_settings()
    await MongoDB.connect(
        uri=settings.MONGODB_URI,
        db_name=settings.MONGODB_DB,
        max_pool_size=settings.MONGODB_MAX_POOL_SIZE,
        min_pool_size=settings.MONGODB_MIN_POOL_SIZE,
    )
    db = MongoDB.get_db()

    checker = GateChecker(db)
    results = await checker.check(args.candidate)
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    logger.info("Gates: %s/%s passed", passed, total)
    for gate, result in results.items():
        logger.info("  %s: %s", "PASS" if result else "FAIL", gate)
    await MongoDB.close()


async def cmd_publish(args):
    from agent.evolution import Publisher
    from config import get_settings
    from db.mongo import MongoDB

    settings = get_settings()
    await MongoDB.connect(
        uri=settings.MONGODB_URI,
        db_name=settings.MONGODB_DB,
        max_pool_size=settings.MONGODB_MAX_POOL_SIZE,
        min_pool_size=settings.MONGODB_MIN_POOL_SIZE,
    )
    db = MongoDB.get_db()

    publisher = Publisher(db)
    await publisher.transition(args.candidate, args.status, args.approver)
    logger.info("Candidate %s -> %s", args.candidate, args.status)
    await MongoDB.close()


def main():
    parser = argparse.ArgumentParser(description="Personalization offline evaluation CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_build = subparsers.add_parser("build", help="Build evaluation dataset")
    p_build.add_argument("--days", type=int, default=90)
    p_build.set_defaults(func=cmd_build)

    p_gen = subparsers.add_parser("generate", help="Generate candidate")
    p_gen.add_argument("--target", required=True, choices=list(EVOLVABLE_TARGETS.keys()))
    p_gen.add_argument("--dataset", required=True)
    p_gen.add_argument("--baseline", required=True)
    p_gen.add_argument("--hypothesis", required=True)
    p_gen.add_argument("--target-failure", action="append", required=True)
    p_gen.add_argument("--metric", default="fitness")
    p_gen.add_argument("--expected-delta", type=float, required=True)
    p_gen.set_defaults(func=cmd_generate)

    p_eval = subparsers.add_parser("evaluate", help="Evaluate candidate")
    p_eval.add_argument("--candidate", required=True)
    p_eval.add_argument("--dataset", required=True)
    p_eval.set_defaults(func=cmd_evaluate)

    p_gates = subparsers.add_parser("gates", help="Check gates")
    p_gates.add_argument("--candidate", required=True)
    p_gates.set_defaults(func=cmd_gates)

    p_pub = subparsers.add_parser("publish", help="Publish candidate")
    p_pub.add_argument("--candidate", required=True)
    p_pub.add_argument("--status", required=True)
    p_pub.add_argument("--approver", required=True)
    p_pub.set_defaults(func=cmd_publish)

    args = parser.parse_args()
    asyncio.run(args.func(args))


# 导入 EVOLVABLE_TARGETS 用于 argparse choices
from agent.evolution import EVOLVABLE_TARGETS  # noqa: E402

if __name__ == "__main__":
    main()
