import asyncio
import httpx
import argparse
import time
import orjson
import statistics
import datetime
import os
import numpy
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
import random

parser = argparse.ArgumentParser()
parser.add_argument('--model', choices=['qwen2.5'])
parser.add_argument('--backend', choices=['vllm', 'lorax', 'tgi'])
parser.add_argument('--batch', nargs='+', type=int, default=[1, 2, 4, 8, 16, 32, 64, 128])
parser.add_argument('--endpoint', nargs="+")
parser.add_argument('--duration', type=int, default=60)
parser.add_argument('--debug', action='store_true')
args = parser.parse_args()


LLAMA_PROMPT = '''<s>[INST] output from 1 to 100 [/INST]'''

def get_endpoint(endpoint):
    if args.backend == 'vllm':
        return endpoint

def get_body():
     if args.backend == 'vllm' and args.model == 'qwen2.5':
        return {
            "model": "QWen2.5-72B-Instruct",
            "messages": [
                {
                  "role": "system",
                  "content": "Your are helpful assistant."
                },
                {
                  "role": "user",
                  "content": "output from 1 to 100"
                }

            ],
           # "stream": True,
            "max_tokens": 1024,
            "temperature": 0
        }
 

async def requests_worker(endpoint: str):
    start = time.perf_counter()
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=None, pool=None, write=None, read=None)) as client:
        endpoint = get_endpoint(endpoint)
        body = get_body()
        tokens = []
        ticks = []
        if args.debug:
            print(body)

        while True:
            ticks.append([time.perf_counter()])
            tokens.append(0)
            sys.stdout.write('.')
            sys.stdout.flush()

            async with client.stream('POST', endpoint, json=body) as r:
                async for chunk in r.aiter_text():
                    tt = time.perf_counter()
                    ticks[-1].append(tt)
                    delta = tt - start
                    if delta > args.duration:
                        return ticks, tokens
                    if chunk == '':
                        continue
                    if args.debug:
                        print(repr(chunk))
                    if args.backend == 'vllm':
                        data = chunk.lstrip("data: ")
                        try:
                            usage = orjson.loads(data)
                            if usage.get("usage", None):
                                tokens[-1] = usage['usage']['completion_tokens']
                        except Exception as e:
                            print(f"json load err {e}")

                    elif args.backend == 'tgi':
                        tokens[-1] += 1

async def batch_worker(batches, endpoint):
    result = []
    for batch_no, batch in enumerate(batches):
        print(f'--- process with batch size {batch}')
        workers = []
        for i in range(batch):
            workers.append(requests_worker(endpoint))
        total_tokens = 0
        first_token_latencies = []
        non_first_token_latency = []
        all_ticks = []
        for tick_group, tokens in await asyncio.gather(*workers):
            total_tokens += sum(tokens)
            all_ticks.append(tick_group)

            for ticks in tick_group:
                if len(ticks) == 1:
                    continue
                diff = numpy.diff(ticks).tolist()
                first_token_latencies.append(diff[0])
                non_first_token_latency.extend(diff[1:])

        if len(non_first_token_latency) > 0:
            result.append({
                'batch': batch,
                'total_token': total_tokens,
                'token_per_s': total_tokens / args.duration,
                'avg_first_token_latency': statistics.mean(first_token_latencies),
                'min_token_latency': min(non_first_token_latency),
                'max_token_latency': max(non_first_token_latency),
                'median_token_latency': statistics.median(non_first_token_latency),
                'avg_token_latency': statistics.mean(non_first_token_latency),
            })
        else:
            result.append({
                'batch': batch,
                'total_token': total_tokens,
                'token_per_s': total_tokens / args.duration,
                'avg_first_token_latency': statistics.mean(first_token_latencies),
            })

        print(result[-1])
        result[-1]['raw_ticks'] = all_ticks
        if batch_no < len(args.batch) - 1:
            await asyncio.sleep(5)
    return result

def run_batch_worker(batches, endpoint):
    random.shuffle(batches)
    return asyncio.run(batch_worker(batches, endpoint))

def main():
    print(args)
    final_result = []
    endpoints = defaultdict(list)
    for batch_no, batch in enumerate(sorted(args.batch)):
        index = batch_no % len(args.endpoint)
        endpoints[args.endpoint[index]].append(batch)
    print(endpoints)
    final_result = []
    with ProcessPoolExecutor(max_workers=len(endpoints)) as pool:
        futures = [pool.submit(run_batch_worker, batches, endpoint) for endpoint, batches in endpoints.items()]
        for i in as_completed(futures):
            final_result.extend(i.result())

    os.makedirs('result', exist_ok=True)
    now_str = datetime.datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    with open(f'result/{now_str}_{args.model}_{args.backend}.json', 'wb') as fp:
        fp.write(orjson.dumps({
            'model': args.model,
            'backend': args.backend,
            "time": now_str,
            "results": final_result,
            "duration": args.duration
        }))

if __name__ == '__main__':
    main()
