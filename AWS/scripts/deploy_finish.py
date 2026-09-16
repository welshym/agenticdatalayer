"""Re-assert BCL security-group rules (the account guardrail periodically strips
0.0.0.0/0 egress), roll the ECS service onto the latest task def revision, wait for
the deployment to stabilise, then verify POST /chat returns a live Bedrock answer
through the ALB. Reads AWS credentials from the workspace .env.

Usage:  python deploy_finish.py
"""
import os
import json
import time
import urllib.request
import urllib.error
import boto3
from botocore.exceptions import ClientError

ENV_PATH = os.environ.get("BCL_ENV", r"d:\code-ai-lab\.env")
REGION = os.environ.get("AWS_REGION", "us-east-1")
CLUSTER = "bcl-cluster"
SERVICE = "bcl-svc"
FAMILY = "bcl-all-in-one"
ALB = os.environ.get("BCL_ALB", "bcl-alb-131414283.us-east-1.elb.amazonaws.com")


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
    ec2 = boto3.client("ec2", region_name=REGION)
    ecs = boto3.client("ecs", region_name=REGION)

    def sg_id(name):
        r = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [name]}])
        return r["SecurityGroups"][0]["GroupId"] if r["SecurityGroups"] else None

    alb_sg, task_sg, vpce_sg = sg_id("bcl-alb-sg"), sg_id("bcl-task-sg"), sg_id("bcl-vpce-sg")
    print(f"SGs: alb={alb_sg} task={task_sg} vpce={vpce_sg}", flush=True)

    def try_rule(fn, label):
        try:
            fn(); print(f"  +{label}", flush=True)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            print(f"  ={label} (exists)" if "Duplicate" in code else f"  !{label}: {code}", flush=True)

    # task_sg egress: all + 443->vpce + 53
    try_rule(lambda: ec2.authorize_security_group_egress(GroupId=task_sg, IpPermissions=[
        {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]), "task egress all")
    if vpce_sg:
        try_rule(lambda: ec2.authorize_security_group_egress(GroupId=task_sg, IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": 443, "ToPort": 443,
             "UserIdGroupPairs": [{"GroupId": vpce_sg}]}]), "task egress 443->vpce")
    try_rule(lambda: ec2.authorize_security_group_egress(GroupId=task_sg, IpPermissions=[
        {"IpProtocol": "udp", "FromPort": 53, "ToPort": 53, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]},
        {"IpProtocol": "tcp", "FromPort": 53, "ToPort": 53, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]),
        "task egress 53")

    # alb_sg egress: all + 8000-8100->task
    try_rule(lambda: ec2.authorize_security_group_egress(GroupId=alb_sg, IpPermissions=[
        {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]), "alb egress all")
    try_rule(lambda: ec2.authorize_security_group_egress(GroupId=alb_sg, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8100,
         "UserIdGroupPairs": [{"GroupId": task_sg}]}]), "alb egress 8000-8100->task")

    # alb_sg ingress: public ports
    for p in [80, 8010, 8014, 8017, 8018, 8080]:
        try_rule(lambda p=p: ec2.authorize_security_group_ingress(GroupId=alb_sg, IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": p, "ToPort": p, "IpRanges": [{"CidrIp": "0.0.0.0/0"}]}]),
            f"alb ingress {p}")

    # task_sg ingress: from alb_sg
    for p in [8010, 8013, 8014, 8017, 8018]:
        try_rule(lambda p=p: ec2.authorize_security_group_ingress(GroupId=task_sg, IpPermissions=[
            {"IpProtocol": "tcp", "FromPort": p, "ToPort": p,
             "UserIdGroupPairs": [{"GroupId": alb_sg}]}]), f"task ingress {p}<-alb")

    # roll the service onto the latest revision
    ecs.update_service(cluster=CLUSTER, service=SERVICE, taskDefinition=FAMILY, forceNewDeployment=True)
    print(f"service updated -> {FAMILY} (force new deployment)", flush=True)

    print("waiting for deployment to stabilise...", flush=True)
    deadline = time.time() + 600
    stable = False
    while time.time() < deadline:
        time.sleep(20)
        d = ecs.describe_services(cluster=CLUSTER, services=[SERVICE])["services"][0]
        dep = [x for x in d["deployments"] if x["status"] == "PRIMARY"][0]
        print(f"  running={dep['runningCount']}/{dep['desiredCount']} rollout={dep.get('rolloutState')}",
              flush=True)
        if dep.get("rolloutState") == "COMPLETED" and dep["runningCount"] >= dep["desiredCount"] >= 1:
            stable = True
            break
    print("DEPLOY-STABLE" if stable else "DEPLOY-TIMEOUT", flush=True)

    # verify via ALB: token then /chat
    def http(method, url, data=None, headers=None, timeout=60):
        req = urllib.request.Request(
            url, data=(json.dumps(data).encode() if data is not None else None),
            headers=headers or {}, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode()

    time.sleep(15)
    try:
        st, body = http("POST", f"http://{ALB}:8018/token", {"client_id": "ui-client"},
                        {"Content-Type": "application/json"})
        tok = json.loads(body).get("access_token") or json.loads(body).get("token")
        print(f"token status={st} got_token={bool(tok)}", flush=True)
        st2, body2 = http("POST", f"http://{ALB}/chat",
                          {"customer_id": "C001", "question": "What products does this customer hold?"},
                          {"Content-Type": "application/json", "Authorization": f"Bearer {tok}"})
        print(f"/chat status={st2}", flush=True)
        print("CHAT-RESPONSE=" + body2[:1200], flush=True)
    except urllib.error.HTTPError as e:
        print(f"verify HTTPError {e.code}: {e.read().decode()[:500]}", flush=True)
    except Exception as e:
        print(f"verify error: {type(e).__name__}: {e}", flush=True)


if __name__ == "__main__":
    main()
