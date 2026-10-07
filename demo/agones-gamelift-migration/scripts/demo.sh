#!/usr/bin/env bash
# Operator script for the Agones-to-GameLift reference environment.
#
# Run it yourself, with the demo operator role (iam/operator-role.yaml), in a
# non-production account. Nothing in the repository runs it for you, and the
# assistant never gets these permissions.
#
# Usage: scripts/demo.sh <command> [args]
#   preflight                      Check tools, identity, and settings
#   cluster-up                     Create the private EKS cluster (about 20 min)
#   agones-up                      Install Agones and the simple-game-server fleet
#   enroll-agent                   Give the assistant read-only access to the cluster
#   restrict-fleet-ingress STACK   Allow only the cluster's NAT address into a GameLift fleet
#   load HOST PORT [PLAYERS] [SECONDS]
#                                  Simulate UDP players from inside the cluster
#   down                           Delete everything the demo created, in order
#   verify-clean                   Fail if any demo resource remains
#
# Settings (environment variables):
#   AWS_REGION       Region for every resource (default us-east-1)
#   CLUSTER_NAME     EKS cluster name (default agones-demo)
#   STACK_PREFIX     GameLift stack name prefix (default gl-demo)
#   REPOSITORY_NAME  ECR repository for the game server image
#                    (default agones-gamelift-demo/game-server)
#   OPERATOR_CIDR    Your workstation's public address as a /32 (required for
#                    cluster-up); the only public source for the Kubernetes API
#   OWNER            Owner tag value (required for cluster-up)
#   MAX_HOURS        Hours until the expires-at tag (default 4, at most 8)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"

REGION="${AWS_REGION:-us-east-1}"
CLUSTER_NAME="${CLUSTER_NAME:-agones-demo}"
STACK_PREFIX="${STACK_PREFIX:-gl-demo}"
REPOSITORY_NAME="${REPOSITORY_NAME:-agones-gamelift-demo/game-server}"
MAX_HOURS="${MAX_HOURS:-4}"
DEMO_TAG_KEY="demo"
DEMO_TAG_VALUE="agones-gamelift-migration"

AGONES_CHART_VERSION="1.61.0"
MIN_EKSCTL_VERSION="0.231.0"
# Group created by infrastructure/kubernetes/enroll-cluster.sh for the default
# runtime role. Override when enrolling a custom role name.
AGENT_GROUP="${AGENT_GROUP:-game-agent-monitoring-group}"
AGENT_ROLE_NAME="${AGENT_ROLE_NAME:-game-agent-agentcore-execution-role}"

die() { echo "error: $*" >&2; exit 1; }
log() { echo "==> $*"; }

require_tools() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
  done
}

version_at_least() {
  # version_at_least HAVE NEED
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]
}

validate_name() {
  [[ "$1" =~ ^[a-z][a-z0-9-]{2,30}$ ]] || die "invalid name: $1"
}

validate_operator_cidr() {
  local cidr="${OPERATOR_CIDR:-}"
  [ -n "$cidr" ] || die "set OPERATOR_CIDR to your workstation's public address as a /32"
  [[ "$cidr" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/(2[4-9]|3[0-2])$ ]] \
    || die "OPERATOR_CIDR must be an IPv4 range of /24 or narrower (got: $cidr)"
  case "$cidr" in
    0.0.0.0/*) die "OPERATOR_CIDR must not be 0.0.0.0" ;;
  esac
}

validate_settings() {
  validate_name "$CLUSTER_NAME"
  [[ "$STACK_PREFIX" =~ ^[a-z][a-z0-9-]{2,20}$ ]] || die "invalid STACK_PREFIX: $STACK_PREFIX"
  [[ "$REGION" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]$ ]] || die "invalid AWS_REGION: $REGION"
  [[ "$MAX_HOURS" =~ ^[1-8]$ ]] || die "MAX_HOURS must be 1-8"
}

expires_at() {
  if date -u -v+1H +%s >/dev/null 2>&1; then
    date -u -v+"${MAX_HOURS}"H +%Y-%m-%dT%H-%M-%SZ  # BSD date
  else
    date -u -d "+${MAX_HOURS} hours" +%Y-%m-%dT%H-%M-%SZ  # GNU date
  fi
}

cmd_preflight() {
  validate_settings
  require_tools aws jq
  local identity
  identity="$(aws sts get-caller-identity --query Arn --output text)" || die "no AWS credentials"
  log "Caller: $identity"
  log "Region: $REGION  Cluster: $CLUSTER_NAME  Stack prefix: $STACK_PREFIX"
  if command -v eksctl >/dev/null 2>&1; then
    local have
    have="$(eksctl version 2>/dev/null | sed -E 's/^([0-9]+\.[0-9]+\.[0-9]+).*/\1/')"
    version_at_least "$have" "$MIN_EKSCTL_VERSION" || die "eksctl $MIN_EKSCTL_VERSION or later is required (found $have)"
  fi
}

cmd_cluster_up() {
  validate_settings
  validate_operator_cidr
  [ -n "${OWNER:-}" ] || die "set OWNER (used for the owner tag)"
  [[ "$OWNER" =~ ^[A-Za-z0-9._@-]{1,64}$ ]] || die "invalid OWNER"
  require_tools eksctl kubectl
  cmd_preflight
  RENDERED_CONFIG="$(mktemp)"
  trap 'rm -f "$RENDERED_CONFIG"' EXIT
  local rendered="$RENDERED_CONFIG"
  sed -e "s|__CLUSTER_NAME__|$CLUSTER_NAME|g" \
      -e "s|__REGION__|$REGION|g" \
      -e "s|__OWNER__|$OWNER|g" \
      -e "s|__EXPIRES_AT__|$(expires_at)|g" \
      -e "s|__OPERATOR_CIDR__|$OPERATOR_CIDR|g" \
      "$DEMO_DIR/eks/cluster.yaml" > "$rendered"
  ! grep -Eq "__[A-Z_]+__" "$rendered" || die "unfilled placeholder in cluster config"
  log "Creating cluster $CLUSTER_NAME (private nodes; API limited to $OPERATOR_CIDR)"
  eksctl create cluster -f "$rendered"
}

cmd_agones_up() {
  require_tools helm kubectl
  log "Installing Agones $AGONES_CHART_VERSION (all services cluster-internal)"
  helm repo add agones https://agones.dev/chart/stable >/dev/null
  helm repo update agones >/dev/null
  helm upgrade --install agones agones/agones --version "$AGONES_CHART_VERSION" \
    --namespace agones-system --create-namespace \
    -f "$DEMO_DIR/agones/helm-values.yaml" --wait --timeout 10m
  kubectl apply -f "$DEMO_DIR/agones/fleet.yaml"
  kubectl wait --for=jsonpath='{.status.readyReplicas}'=3 fleet/simple-game-server --timeout=5m
  local public
  public="$(kubectl get svc -A -o jsonpath='{range .items[?(@.spec.type=="LoadBalancer")]}{.metadata.name}{"\n"}{end}')"
  [ -z "$public" ] || die "unexpected LoadBalancer services: $public"
}

cmd_enroll_agent() {
  require_tools kubectl
  log "Enrolling the assistant's runtime role (read-only core resources)"
  bash "$REPO_ROOT/infrastructure/kubernetes/enroll-cluster.sh" "$CLUSTER_NAME" "$REGION" --role-name "$AGENT_ROLE_NAME"
  log "Adding read-only Agones resources for group $AGENT_GROUP"
  sed "s|__AGENT_GROUP__|$AGENT_GROUP|" "$DEMO_DIR/agones/agent-readonly-rbac.yaml" | kubectl apply -f -
}

nat_egress_cidr() {
  local ip
  ip="$(aws ec2 describe-nat-gateways --region "$REGION" \
    --filter "Name=tag:alpha.eksctl.io/cluster-name,Values=$CLUSTER_NAME" "Name=state,Values=available" \
    --query 'NatGateways[0].NatGatewayAddresses[0].PublicIp' --output text)"
  [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die "could not find the cluster's NAT gateway address"
  echo "$ip/32"
}

cmd_restrict_fleet_ingress() {
  local stack="${1:-}"
  [[ "$stack" == "$STACK_PREFIX"-* ]] || die "usage: restrict-fleet-ingress ${STACK_PREFIX}-<name>"
  local fleet_id from to cidr
  fleet_id="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$stack" \
    --query "Stacks[0].Outputs[?OutputKey=='FleetId'].OutputValue" --output text)"
  [[ "$fleet_id" == containerfleet-* ]] || die "stack $stack has no FleetId output for a container fleet"
  read -r from to < <(aws gamelift describe-container-fleet --region "$REGION" --fleet-id "$fleet_id" \
    --query 'ContainerFleet.InstanceConnectionPortRange.[FromPort,ToPort]' --output text)
  cidr="$(nat_egress_cidr)"
  log "Fleet $fleet_id: allowing UDP $from-$to from $cidr only"
  aws gamelift update-container-fleet --region "$REGION" --fleet-id "$fleet_id" \
    --instance-inbound-permission-authorizations "[{\"FromPort\":$from,\"ToPort\":$to,\"IpRange\":\"$cidr\",\"Protocol\":\"UDP\"}]" \
    --instance-inbound-permission-revocations "[{\"FromPort\":$from,\"ToPort\":$to,\"IpRange\":\"0.0.0.0/0\",\"Protocol\":\"UDP\"}]" \
    >/dev/null
}

cmd_load() {
  local host="${1:-}" port="${2:-}" players="${3:-8}" seconds="${4:-30}"
  [[ "$host" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die "usage: load HOST PORT [PLAYERS] [SECONDS]"
  [[ "$port" =~ ^[0-9]{1,5}$ && "$players" =~ ^[0-9]{1,3}$ && "$seconds" =~ ^[0-9]{1,4}$ ]] || die "invalid load arguments"
  require_tools kubectl
  kubectl delete job udp-load --ignore-not-found >/dev/null
  sed -e "s|REPLACE_WITH_GAMESERVER_POD_IP|$host|" \
      -e "s|value: \"7654\"|value: \"$port\"|" \
      -e "s|value: \"10\"|value: \"$players\"|" \
      -e "s|value: \"120\"|value: \"$seconds\"|" \
      "$DEMO_DIR/client/load-job.yaml" | kubectl apply -f -
  kubectl wait --for=condition=complete job/udp-load --timeout="$((seconds + 120))s"
  kubectl logs job/udp-load | tail -n 3
}

demo_stacks() {
  aws cloudformation list-stacks --region "$REGION" \
    --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE ROLLBACK_COMPLETE UPDATE_ROLLBACK_COMPLETE \
      CREATE_FAILED DELETE_FAILED UPDATE_ROLLBACK_FAILED \
    --query "StackSummaries[?starts_with(StackName, '${STACK_PREFIX}-')].StackName" --output text
}

cmd_down() {
  validate_settings
  local stack
  # 1. GameLift stacks first: fleets bill per instance hour.
  for stack in $(demo_stacks); do
    log "Deleting GameLift stack $stack"
    aws cloudformation delete-stack --region "$REGION" --stack-name "$stack"
  done
  for stack in $(demo_stacks); do
    aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$stack"
  done
  # 2. Kubernetes workloads, so no load balancer or volume outlives the cluster.
  if command -v kubectl >/dev/null 2>&1 && kubectl get ns agones-system >/dev/null 2>&1; then
    log "Removing the fleet and Agones"
    kubectl delete -f "$DEMO_DIR/agones/fleet.yaml" --ignore-not-found --wait=true || true
    helm uninstall agones --namespace agones-system --wait || true
  fi
  # 3. The cluster, its node group, VPC, and NAT gateway (eksctl stacks).
  if aws eks describe-cluster --region "$REGION" --name "$CLUSTER_NAME" >/dev/null 2>&1; then
    log "Deleting cluster $CLUSTER_NAME"
    eksctl delete cluster --region "$REGION" --name "$CLUSTER_NAME" --disable-nodegroup-eviction --wait
  fi
  # 4. The image repository.
  if aws ecr describe-repositories --region "$REGION" --repository-names "$REPOSITORY_NAME" >/dev/null 2>&1; then
    log "Deleting ECR repository $REPOSITORY_NAME"
    aws ecr delete-repository --region "$REGION" --repository-name "$REPOSITORY_NAME" --force >/dev/null
  fi
  cmd_verify_clean
}

ec2_resource_exists() {
  # The tagging API keeps listing deleted EC2 resources for a while (terminated
  # instances, deleted NAT gateways, volumes, subnets). Ask EC2 directly. Any
  # answer other than "gone" counts as present, so a failed lookup is reported.
  local arn="$1" id kind state
  local -a describe
  id="${arn##*/}"
  kind="${arn#*:ec2:*:*:}"; kind="${kind%%/*}"
  case "$kind" in
    instance) describe=(describe-instances --instance-ids "$id" --query 'Reservations[0].Instances[0].State.Name') ;;
    natgateway) describe=(describe-nat-gateways --nat-gateway-ids "$id" --query 'NatGateways[0].State') ;;
    volume) describe=(describe-volumes --volume-ids "$id" --query 'Volumes[0].State') ;;
    network-interface) describe=(describe-network-interfaces --network-interface-ids "$id" --query 'NetworkInterfaces[0].Status') ;;
    subnet) describe=(describe-subnets --subnet-ids "$id" --query 'Subnets[0].State') ;;
    vpc) describe=(describe-vpcs --vpc-ids "$id" --query 'Vpcs[0].State') ;;
    security-group) describe=(describe-security-groups --group-ids "$id" --query 'SecurityGroups[0].GroupId') ;;
    route-table) describe=(describe-route-tables --route-table-ids "$id" --query 'RouteTables[0].RouteTableId') ;;
    internet-gateway) describe=(describe-internet-gateways --internet-gateway-ids "$id" --query 'InternetGateways[0].InternetGatewayId') ;;
    elastic-ip) describe=(describe-addresses --allocation-ids "$id" --query 'Addresses[0].AllocationId') ;;
    launch-template) describe=(describe-launch-templates --launch-template-ids "$id" --query 'LaunchTemplates[0].LaunchTemplateId') ;;
    *) return 0 ;;  # Unknown type: report it rather than guess.
  esac
  state="$(aws ec2 "${describe[@]}" --region "$REGION" --output text 2>&1)" || true
  case "$state" in
    *NotFound*|None|terminated|deleted) return 1 ;;
  esac
  return 0
}

tagged_resources() {
  # demo-env scopes the check to this environment, so another copy of the demo
  # in the same account is neither reported nor touched.
  local arn
  for arn in $(aws resourcegroupstaggingapi get-resources --region "$REGION" \
    --tag-filters "Key=$DEMO_TAG_KEY,Values=$DEMO_TAG_VALUE" "Key=demo-env,Values=$CLUSTER_NAME" \
    --query 'ResourceTagMappingList[].ResourceARN' --output text); do
    [ "$arn" != "None" ] || continue
    if [[ "$arn" == arn:*:ec2:* ]] && ! ec2_resource_exists "$arn"; then
      continue
    fi
    echo "$arn"
  done
}

cmd_verify_clean() {
  validate_settings
  local remaining=0 found attempt
  local attempts="${VERIFY_ATTEMPTS:-5}" delay="${VERIFY_DELAY_SECONDS:-60}"
  # The tagging API is eventually consistent; only report resources that are
  # still listed after several checks.
  for ((attempt = 1; attempt <= attempts; attempt++)); do
    found="$(tagged_resources)"
    [ -n "$found" ] || break
    [ "$attempt" -lt "$attempts" ] && sleep "$delay"
  done
  if [ -n "$found" ]; then
    echo "Tagged demo resources remain:"; echo "$found"; remaining=1
  fi
  found="$(demo_stacks)"
  [ -z "$found" ] || { echo "GameLift stacks remain: $found"; remaining=1; }
  found="$(aws cloudformation list-stacks --region "$REGION" \
    --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE DELETE_FAILED ROLLBACK_COMPLETE \
    --query "StackSummaries[?starts_with(StackName, 'eksctl-${CLUSTER_NAME}-')].StackName" --output text)"
  [ -z "$found" ] || { echo "eksctl stacks remain: $found"; remaining=1; }
  if aws eks describe-cluster --region "$REGION" --name "$CLUSTER_NAME" >/dev/null 2>&1; then
    echo "Cluster remains: $CLUSTER_NAME"; remaining=1
  fi
  if aws ecr describe-repositories --region "$REGION" --repository-names "$REPOSITORY_NAME" >/dev/null 2>&1; then
    echo "ECR repository remains: $REPOSITORY_NAME"; remaining=1
  fi
  [ "$remaining" -eq 0 ] || die "demo resources remain (see above)"
  log "No demo resources remain in $REGION"
}

main() {
  local command="${1:-}"
  shift || true
  case "$command" in
    preflight) cmd_preflight ;;
    cluster-up) cmd_cluster_up ;;
    agones-up) cmd_agones_up ;;
    enroll-agent) cmd_enroll_agent ;;
    restrict-fleet-ingress) cmd_restrict_fleet_ingress "$@" ;;
    load) cmd_load "$@" ;;
    down) cmd_down ;;
    verify-clean) cmd_verify_clean ;;
    *) sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; [ -z "$command" ] || exit 2 ;;
  esac
}

main "$@"
