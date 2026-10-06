"""Authenticated API admission/load probe. Runs only against the explicitly supplied URL.

Creates real queued research jobs: use a dedicated staging account and bounded worker budget.
Requires httpx. Outputs measured admission latency, throughput and failure rate, not LLM quality.
"""
import argparse
import asyncio
import json
import os
import statistics
import time
from uuid import uuid4

import httpx


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--requests", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()
    if not 1 <= args.requests <= 100 or not 1 <= args.concurrency <= 20:
        parser.error("requests must be 1-100 and concurrency 1-20")
    token = os.environ["MARS_LOAD_TOKEN"]
    semaphore = asyncio.Semaphore(args.concurrency)
    latencies, outcomes = [], []
    start = time.perf_counter()
    async with httpx.AsyncClient(base_url=args.url, timeout=30, headers={"Authorization": f"Bearer {token}", "X-MARS-Client": "1"}) as client:
        async def submit(index):
            async with semaphore:
                began = time.perf_counter()
                response = await client.post("/api/v1/research", headers={"Idempotency-Key": str(uuid4())}, json={"query": f"What evidence supports reproducible research methods? Trial {index}"})
                latencies.append((time.perf_counter() - began) * 1000)
                outcomes.append(response.status_code)
        await asyncio.gather(*(submit(i) for i in range(args.requests)))
    values = sorted(latencies)
    def percentile(p):
        return values[min(len(values)-1, int((len(values)-1)*p))]
    print(json.dumps({"requests": len(values), "p50_ms": statistics.median(values), "p95_ms": percentile(.95), "p99_ms": percentile(.99),
                      "requests_per_second": len(values)/(time.perf_counter()-start), "failure_rate": sum(s != 202 for s in outcomes)/len(outcomes),
                      "statuses": {str(s): outcomes.count(s) for s in set(outcomes)}}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
