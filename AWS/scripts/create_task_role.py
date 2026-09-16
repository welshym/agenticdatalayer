"""Create the bcl-task-role (trust ecs-tasks) with Bedrock invoke permissions, then
register a new bcl-all-in-one task definition revision that uses the role and sets
BEDROCK_MODEL. Idempotent. Reads AWS credentials from the workspace .env.

Usage:  python create_task_role.py
Env:    BEDROCK_MODEL (default amazon.nova-lite-v1:0)
"""
import os
import json
import time
import boto3
from botocore.exceptions import ClientError

ENV_PATH = os.environ.get("BCL_ENV", r"d:\code-ai-lab\.env")
REGION = os.environ.get("AWS_REGION", "us-east-1")
ROLE = "bcl-task-role"
FAMILY = "bcl-all-in-one"
MODEL = os.environ.get("BEDROCK_MODEL", "amazon.nova-lite-v1:0")
HERE = os.path.dirname(os.path.abspath(__file__))
IAM_DIR = os.path.normpath(os.path.join(HERE, "..", "iam"))


def load_env(path):
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


def main():
    load_env(ENV_PATH)
    with open(os.path.join(IAM_DIR, "bcl-task-role-trust-policy.json")) as f:
        trust = f.read()
    with open(os.path.join(IAM_DIR, "bcl-bedrock-invoke-policy.json")) as f:
        policy = f.read()

    iam = boto3.client("iam", region_name=REGION)
    try:
        role_arn = iam.create_role(
            RoleName=ROLE,
            AssumeRolePolicyDocument=trust,
            Description="BCL ECS task role for Bedrock chat",
            Tags=[{"Key": "Project", "Value": "BCL"}],
        )["Role"]["Arn"]
        print(f"created role {role_arn}", flush=True)
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            role_arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]
            print(f"role exists {role_arn}", flush=True)
        else:
            raise
    iam.put_role_policy(RoleName=ROLE, PolicyName="bcl-bedrock-invoke", PolicyDocument=policy)
    print("inline policy bcl-bedrock-invoke attached", flush=True)
    time.sleep(8)  # let the new role propagate before referencing it

    ecs = boto3.client("ecs", region_name=REGION)
    d = ecs.describe_task_definition(taskDefinition=FAMILY, include=["TAGS"])
    td = d["taskDefinition"]
    tags = d.get("tags") or [{"key": "Project", "value": "BCL"}]

    for c in td["containerDefinitions"]:
        env = {e["name"]: e["value"] for e in c.get("environment", [])}
        env.update({"BEDROCK_MODEL": MODEL, "AWS_REGION": REGION, "AWS_DEFAULT_REGION": REGION})
        c["environment"] = [{"name": k, "value": v} for k, v in env.items()]

    kw = dict(
        family=td["family"],
        taskRoleArn=role_arn,
        executionRoleArn=td["executionRoleArn"],
        networkMode=td["networkMode"],
        containerDefinitions=td["containerDefinitions"],
        requiresCompatibilities=td.get("requiresCompatibilities", ["FARGATE"]),
        cpu=td["cpu"],
        memory=td["memory"],
        tags=tags,
    )
    if td.get("runtimePlatform"):
        kw["runtimePlatform"] = td["runtimePlatform"]
    if td.get("volumes"):
        kw["volumes"] = td["volumes"]
    new = ecs.register_task_definition(**kw)["taskDefinition"]
    print(f"REGISTERED={new['taskDefinitionArn']}", flush=True)
    print(f"MODEL={MODEL}", flush=True)


if __name__ == "__main__":
    main()
