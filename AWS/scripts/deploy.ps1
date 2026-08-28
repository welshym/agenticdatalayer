# BCL deploy: refresh AWS session, build + push the all-in-one image to ECR, then
# re-assert security groups, roll the ECS service, and verify /chat (deploy_finish.py).
#
# Prerequisites: Docker Desktop, AWS CLI, Python 3.12 with boto3, and the MFA login
# skill at d:\code-ai-lab\.kiro\skills\aws-mfa-login\connect.py.
#
# Usage:  powershell -ExecutionPolicy Bypass -File AWS/scripts/deploy.ps1

$ErrorActionPreference = 'Continue'
$repoRoot = Resolve-Path (Join-Path $PSScriptRoot '..\..')
$envFile  = 'd:\code-ai-lab\.env'
$reg      = '054663422011.dkr.ecr.us-east-1.amazonaws.com'
$img      = "$reg/bcl/all-in-one:latest"
$dockerfile = Join-Path $repoRoot 'deploy\docker\Dockerfile'

# 1. Refresh the short-lived MFA session (writes creds to .env)
python 'd:\code-ai-lab\.kiro\skills\aws-mfa-login\connect.py'

# 2. Load refreshed creds into this session for aws/docker
Get-Content $envFile | ForEach-Object {
  if ($_ -match '^\s*([^#=]+)=(.*)$') {
    [Environment]::SetEnvironmentVariable($matches[1].Trim(), $matches[2].Trim())
  }
}

# 3. Build + push
Write-Output '=== ECR LOGIN ==='
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin $reg

Write-Output '=== DOCKER BUILD ==='
docker build -t $img -f $dockerfile $repoRoot
Write-Output "BUILD-EXIT=$LASTEXITCODE"

Write-Output '=== DOCKER PUSH ==='
docker push $img
Write-Output "PUSH-EXIT=$LASTEXITCODE"

# 4. Re-assert SGs, roll the service, verify /chat
Write-Output '=== FINISH (SG + service roll + verify) ==='
python (Join-Path $PSScriptRoot 'deploy_finish.py')
Write-Output 'DEPLOY-DONE'
