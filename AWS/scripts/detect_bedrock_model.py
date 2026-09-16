"""Detect which Amazon Bedrock text models are invokable in this account/region.

Tries the unified Converse API against a shortlist of candidate models (including
`us.` cross-region inference-profile prefixes) and prints a WINNER=<modelId> line
for the first that responds. Reads AWS credentials from the workspace .env.

Usage:  python detect_bedrock_model.py
"""
import os
import boto3

ENV_PATH = os.environ.get("BCL_ENV", r"d:\code-ai-lab\.env")
REGION = os.environ.get("AWS_REGION", "us-east-1")

CANDIDATES = [
    "amazon.nova-micro-v1:0", "us.amazon.nova-micro-v1:0",
    "amazon.nova-lite-v1:0",  "us.amazon.nova-lite-v1:0",
    "amazon.titan-text-express-v1", "amazon.titan-text-lite-v1",
    "anthropic.claude-3-5-haiku-20241022-v1:0", "us.anthropic.claude-3-5-haiku-20241022-v1:0",
    "anthropic.claude-3-haiku-20240307-v1:0",   "us.anthropic.claude-3-haiku-20240307-v1:0",
    "meta.llama3-8b-instruct-v1:0",
]


def load_env(path):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def main():
    load_env(ENV_PATH)
    session = boto3.Session(region_name=REGION)
    rt = session.client("bedrock-runtime")
    winner = None
    for model_id in CANDIDATES:
        try:
            resp = rt.converse(
                modelId=model_id,
                messages=[{"role": "user", "content": [{"text": "Reply with OK"}]}],
                inferenceConfig={"maxTokens": 20, "temperature": 0},
            )
            text = resp["output"]["message"]["content"][0]["text"].strip()
            print(f'[OK]  {model_id} -> "{text}"', flush=True)
            if winner is None:
                winner = model_id
        except Exception as exc:
            print(f"[no] {model_id} -> {type(exc).__name__}", flush=True)
    print(f"WINNER={winner}", flush=True)


if __name__ == "__main__":
    main()
