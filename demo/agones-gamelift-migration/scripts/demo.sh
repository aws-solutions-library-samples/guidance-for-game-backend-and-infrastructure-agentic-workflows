#!/usr/bin/env bash
# Operator script for the Agones-to-GameLift reference environment.
#
# Run it yourself, with the demo operator role (iam/operator-role.yaml), in a
# dedicated sandbox account. Nothing in the repository runs it for you, and the
# assistant never gets these permissions.
#
# Usage: scripts/demo.sh <command> [args]
#   preflight                      Check tools, identity, and settings
#   cluster-up                     Create the private EKS cluster (about 20 min)
#   agones-up                      Install Agones and the simple-game-server fleet
#   enroll-agent                   Give the assistant read-only access to the cluster
#   kubeconfig                     Write the demo-owned kubeconfig and print its path
#   nat-cidr                       Print the cluster's NAT gateway address as a /32
#   boundary-arn                   Print the workload permissions boundary ARN
#   restrict-fleet-ingress STACK   Allow only the cluster's NAT address into a GameLift fleet
#   load HOST PORT [PLAYERS] [SECONDS]
#                                  Simulate UDP players from inside the cluster
#   down                           Delete everything this environment created, in order
#   verify-clean                   Fail if any resource of this environment remains
#
# Settings (environment variables):
#   AWS_REGION       Region for every resource (default us-east-1)
#   CLUSTER_NAME     EKS cluster name and demo-env tag value (default agones-demo)
#   STACK_PREFIX     GameLift stack name prefix (default gl-demo)
#   REPOSITORY_NAME  ECR repository for the game server image
#                    (default agones-gamelift-demo/game-server)
#   OPERATOR_CIDR    Your workstation's public IPv4 address as a /32 (required for
#                    cluster-up); the only public source for the Kubernetes API
#   OWNER            Owner tag value (required for cluster-up)
#   MAX_HOURS        Hours until the expires-at tag (default 4, at most 8)
#   DEMO_KUBECONFIG  Kubeconfig this script owns (default ~/.kube/agones-demo-<cluster>)
#   DEMO_CONFIRM     Set to the cluster name to run `down` without a prompt
#   AGENT_ROLE_NAME  The assistant's runtime role (default game-agent-agentcore-execution-role)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"

REGION="${AWS_REGION:-us-east-1}"
CLUSTER_NAME="${CLUSTER_NAME:-agones-demo}"
STACK_PREFIX="${STACK_PREFIX:-gl-demo}"
REPOSITORY_NAME="${REPOSITORY_NAME:-agones-gamelift-demo/game-server}"
MAX_HOURS="${MAX_HOURS:-4}"
DEMO_KUBECONFIG="${DEMO_KUBECONFIG:-$HOME/.kube/agones-demo-$CLUSTER_NAME}"
DEMO_TAG_KEY="demo"
DEMO_TAG_VALUE="agones-gamelift-migration"

AGONES_CHART_VERSION="1.61.0"
MIN_EKSCTL_VERSION="0.231.0"
AGENT_ROLE_NAME="${AGENT_ROLE_NAME:-game-agent-agentcore-execution-role}"
# Status aws_lookup returns when the service reports the resource does not
# exist. Any other failure exits the script, even inside $(...).
NOT_FOUND=10
# Status review_stack_tags returns when change sets disagree on this
# environment's tags (distinct from 1, which aws_lookup uses for AWS errors).
CHANGE_SETS_DISAGREE=11

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

valid_ipv4() {
  local ip="$1" octet
  [[ "$ip" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  for octet in "${BASH_REMATCH[@]:1}"; do
    [ "$octet" -le 255 ] || return 1
  done
}

valid_port() {
  [[ "$1" =~ ^[1-9][0-9]{0,4}$ ]] && [ "$1" -le 65535 ]
}

validate_operator_cidr() {
  local cidr="${OPERATOR_CIDR:-}"
  [ -n "$cidr" ] || die "set OPERATOR_CIDR to your workstation's public IPv4 address as a /32"
  [[ "$cidr" == */32 ]] && valid_ipv4 "${cidr%/32}" || die "OPERATOR_CIDR must be a single IPv4 address as a /32 (got: $cidr)"
  [ "${cidr%/32}" != "0.0.0.0" ] || die "OPERATOR_CIDR must not be 0.0.0.0"
}

validate_settings() {
  [[ "$CLUSTER_NAME" =~ ^[a-z][a-z0-9-]{2,30}$ ]] || die "invalid CLUSTER_NAME: $CLUSTER_NAME"
  [[ "$STACK_PREFIX" =~ ^[a-z][a-z0-9-]{2,20}$ ]] || die "invalid STACK_PREFIX: $STACK_PREFIX"
  [[ "$REGION" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]$ ]] || die "invalid AWS_REGION: $REGION"
  [[ "$MAX_HOURS" =~ ^[1-8]$ ]] || die "MAX_HOURS must be 1-8"
  [[ "$AGENT_ROLE_NAME" =~ ^[A-Za-z0-9+=,.@_-]{1,64}$ ]] || die "invalid AGENT_ROLE_NAME: $AGENT_ROLE_NAME"
}

expires_at() {
  if date -u -v+1H +%s >/dev/null 2>&1; then
    date -u -v+"${MAX_HOURS}"H +%Y-%m-%dT%H-%M-%SZ  # BSD date
  else
    date -u -d "+${MAX_HOURS} hours" +%Y-%m-%dT%H-%M-%SZ  # GNU date
  fi
}

# ---------------------------------------------------------------------------
# AWS lookups that never treat an error as "absent"
# ---------------------------------------------------------------------------

# aws_lookup NOT_FOUND_PATTERN aws-args...
# Prints the command's output. Returns $NOT_FOUND only when the service reports
# the resource does not exist; any other failure (denied, throttled, expired
# credentials) exits with status 1. Callers that capture the output must
# check the status with absent_or_exit, because an exit inside $(...) ends
# only the subshell.
aws_lookup() {
  local not_found="$1" output
  shift
  if output="$(aws "$@" 2>&1)"; then
    printf '%s' "$output"
    return 0
  fi
  if grep -Eq "$not_found" <<<"$output"; then
    return "$NOT_FOUND"
  fi
  die "AWS call failed (aws $1 $2): $(head -c 300 <<<"$output")"
}

# absent_or_exit STATUS: continue only if STATUS is aws_lookup's not-found.
absent_or_exit() {
  [ "$1" -eq "$NOT_FOUND" ] || exit "$1"
}

# Prints "ACCOUNT PARTITION" for the caller.
caller_account_partition() {
  local identity account arn
  identity="$(aws_lookup 'NOT_A_MATCH' sts get-caller-identity --query '[Account,Arn]' --output text)" || exit 1
  read -r account arn <<<"$identity"
  [[ "$account" =~ ^[0-9]{12}$ && "$arn" == arn:* ]] || die "unexpected caller identity"
  echo "$account $(cut -d: -f2 <<<"$arn")"
}

# Prints the workload boundary ARN after checking that the operator-role stack
# created it; the operator role cannot create a role without it.
workload_boundary_arn() {
  local identity account partition arn
  identity="$(caller_account_partition)" || exit 1
  read -r account partition <<<"$identity"
  arn="arn:${partition}:iam::${account}:policy/${CLUSTER_NAME}-workload-boundary"
  if ! aws_lookup 'NoSuchEntity' iam get-policy --policy-arn "$arn" >/dev/null; then
    die "permissions boundary $arn does not exist; deploy iam/operator-role.yaml with ClusterName=$CLUSTER_NAME first"
  fi
  echo "$arn"
}

# Prints the cluster's tags as JSON; returns $NOT_FOUND if the cluster does not exist.
cluster_tags() {
  aws_lookup 'ResourceNotFoundException' eks describe-cluster --region "$REGION" --name "$CLUSTER_NAME" \
    --query 'cluster.tags' --output json
}

tags_belong_to_demo() {
  # tags_belong_to_demo JSON  (object {"k":"v"} or list [{"Key":..,"Value":..}])
  jq -e --arg k "$DEMO_TAG_KEY" --arg v "$DEMO_TAG_VALUE" --arg env "$CLUSTER_NAME" '
    (if type == "array" then from_entries else . end) as $t
    | ($t[$k] == $v) and ($t["demo-env"] == $env)' >/dev/null <<<"$1"
}

# Prints the repository ARN; returns $NOT_FOUND if the repository does not exist.
repository_arn() {
  aws_lookup 'RepositoryNotFoundException' ecr describe-repositories --region "$REGION" \
    --repository-names "$REPOSITORY_NAME" --query 'repositories[0].repositoryArn' --output text
}

repository_tags() {
  aws_lookup 'NOT_A_MATCH' ecr list-tags-for-resource --region "$REGION" --resource-arn "$1" \
    --query 'tags' --output json
}

# Every stack with the prefix, in any status except DELETE_COMPLETE, as
# "name status" lines.
stacks_with_prefix() {
  local prefix="$1"
  aws_lookup 'NOT_A_MATCH' cloudformation list-stacks --region "$REGION" \
    --query "StackSummaries[?StackStatus!='DELETE_COMPLETE' && starts_with(StackName, '${prefix}')].[StackName,StackStatus]" \
    --output text
}

stack_tags() {
  aws_lookup 'does not exist' cloudformation describe-stacks --region "$REGION" --stack-name "$1" \
    --query 'Stacks[0].Tags' --output json
}

# A stack in REVIEW_IN_PROGRESS (a change set never executed) has no stack
# tags yet; its tags are on the change sets. Prints them as one JSON list, or
# returns $CHANGE_SETS_DISAGREE if the change sets disagree on the demo and
# demo-env tags, so a stack counts as this environment's only when every change
# set says so. Any AWS error exits as usual, so callers fail closed.
review_stack_tags() {
  local ids id tags first=""
  ids="$(aws_lookup 'does not exist' cloudformation list-change-sets --region "$REGION" --stack-name "$1" \
    --query 'Summaries[].ChangeSetId' --output text)" || return "$?"
  for id in $ids; do
    [ "$id" != "None" ] || continue
    tags="$(aws_lookup 'does not exist' cloudformation describe-change-set --region "$REGION" --change-set-name "$id" \
      --query 'Tags' --output json)" || return "$?"
    # Compare only the tags that decide ownership; other keys may differ.
    tags="$(jq -c '[(. // [])[] | select(.Key == "demo" or .Key == "demo-env")] | sort_by(.Key)' <<<"$tags")"
    if [ -z "$first" ]; then first="$tags"; elif [ "$tags" != "$first" ]; then return "$CHANGE_SETS_DISAGREE"; fi
  done
  printf '%s' "${first:-[]}"
}

# Stacks of THIS environment: prefix match AND demo/demo-env tags.
# demo_stacks [PREFIX]  (default: the GameLift stack prefix)
demo_stacks() {
  local prefix="${1:-${STACK_PREFIX}-}" listing name status tags
  listing="$(stacks_with_prefix "$prefix")" || exit 1
  while read -r name status; do
    [ -n "$name" ] && [ "$name" != "None" ] || continue
    if tags="$(stack_tags "$name")"; then
      if [ "$status" = REVIEW_IN_PROGRESS ] && jq -e '(. // []) | length == 0' >/dev/null <<<"$tags"; then
        if tags="$(review_stack_tags "$name")"; then :; else
          local rc="$?"
          [ "$rc" -eq "$CHANGE_SETS_DISAGREE" ] && continue  # not provably ours
          absent_or_exit "$rc"; continue
        fi
      fi
      if tags_belong_to_demo "$tags"; then
        echo "$name $status"
      fi
    else
      absent_or_exit "$?"  # deleted since listing
    fi
  done <<<"$listing"
}

# ---------------------------------------------------------------------------
# Kubernetes access: a kubeconfig this script owns, checked against the cluster
# ---------------------------------------------------------------------------

use_demo_kube() {
  require_tools kubectl
  local tags endpoint server
  if tags="$(cluster_tags)"; then :; else
    absent_or_exit "$?"
    die "cluster $CLUSTER_NAME does not exist"
  fi
  tags_belong_to_demo "$tags" || die "cluster $CLUSTER_NAME is not tagged demo-env=$CLUSTER_NAME; refusing to touch it"
  mkdir -p "$(dirname "$DEMO_KUBECONFIG")"
  aws_lookup 'NOT_A_MATCH' eks update-kubeconfig --region "$REGION" --name "$CLUSTER_NAME" \
    --kubeconfig "$DEMO_KUBECONFIG" >/dev/null
  export KUBECONFIG="$DEMO_KUBECONFIG"
  endpoint="$(aws_lookup 'NOT_A_MATCH' eks describe-cluster --region "$REGION" --name "$CLUSTER_NAME" \
    --query 'cluster.endpoint' --output text)"
  server="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')"
  [[ "$endpoint" == https://* ]] && [ "$server" = "$endpoint" ] \
    || die "kubeconfig server ($server) does not match cluster $CLUSTER_NAME ($endpoint)"
}

cmd_kubeconfig() {
  validate_settings
  use_demo_kube >&2
  echo "$DEMO_KUBECONFIG"
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

cmd_preflight() {
  validate_settings
  require_tools aws jq
  local identity
  identity="$(aws_lookup 'NOT_A_MATCH' sts get-caller-identity --query Arn --output text)"
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
  local boundary
  boundary="$(workload_boundary_arn)"
  RENDERED_CONFIG="$(mktemp)"
  trap 'rm -f "$RENDERED_CONFIG"' EXIT
  local rendered="$RENDERED_CONFIG"
  sed -e "s|__CLUSTER_NAME__|$CLUSTER_NAME|g" \
      -e "s|__REGION__|$REGION|g" \
      -e "s|__OWNER__|$OWNER|g" \
      -e "s|__EXPIRES_AT__|$(expires_at)|g" \
      -e "s|__OPERATOR_CIDR__|$OPERATOR_CIDR|g" \
      -e "s|__BOUNDARY_ARN__|$boundary|g" \
      "$DEMO_DIR/eks/cluster.yaml" > "$rendered"
  ! grep -Eq "__[A-Z_]+__" "$rendered" || die "unfilled placeholder in cluster config"
  mkdir -p "$(dirname "$DEMO_KUBECONFIG")"
  log "Creating cluster $CLUSTER_NAME (private nodes; API limited to $OPERATOR_CIDR)"
  eksctl create cluster -f "$rendered" --kubeconfig "$DEMO_KUBECONFIG"
}

cmd_agones_up() {
  validate_settings
  require_tools helm
  use_demo_kube
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

# The single Kubernetes group enroll-cluster.sh gave the assistant's role.
agent_group() {
  local identity account partition groups group
  identity="$(caller_account_partition)" || exit 1
  read -r account partition <<<"$identity"
  groups="$(aws_lookup 'NOT_A_MATCH' eks describe-access-entry --region "$REGION" --cluster-name "$CLUSTER_NAME" \
    --principal-arn "arn:${partition}:iam::${account}:role/${AGENT_ROLE_NAME}" \
    --query 'accessEntry.kubernetesGroups' --output json)" || exit 1
  group="$(jq -r 'if type == "array" and length == 1 then .[0] else "" end' <<<"$groups")"
  [[ "$group" =~ ^game-agent-monitoring-[a-z0-9-]{1,40}$ ]] \
    || die "expected exactly one game-agent-monitoring-* group on the assistant's access entry (got: $groups)"
  echo "$group"
}

cmd_enroll_agent() {
  validate_settings
  require_tools jq
  use_demo_kube
  log "Enrolling the assistant's runtime role (read-only core resources)"
  bash "$REPO_ROOT/infrastructure/kubernetes/enroll-cluster.sh" "$CLUSTER_NAME" "$REGION" --role-name "$AGENT_ROLE_NAME"
  local group
  group="$(agent_group)"
  log "Adding read-only Agones resources for group $group"
  sed "s|__AGENT_GROUP__|$group|" "$DEMO_DIR/agones/agent-readonly-rbac.yaml" | kubectl apply -f -
}

nat_egress_cidr() {
  local ip
  ip="$(aws_lookup 'NOT_A_MATCH' ec2 describe-nat-gateways --region "$REGION" \
    --filter "Name=tag:alpha.eksctl.io/cluster-name,Values=$CLUSTER_NAME" "Name=state,Values=available" \
    --query 'NatGateways[0].NatGatewayAddresses[0].PublicIp' --output text)" || exit 1
  valid_ipv4 "$ip" || die "could not find the cluster's NAT gateway address"
  echo "$ip/32"
}

cmd_nat_cidr() {
  validate_settings
  nat_egress_cidr
}

cmd_boundary_arn() {
  validate_settings
  workload_boundary_arn
}

cmd_restrict_fleet_ingress() {
  validate_settings
  local stack="${1:-}"
  [[ "$stack" == "$STACK_PREFIX"-* ]] || die "usage: restrict-fleet-ingress ${STACK_PREFIX}-<name>"
  local tags fleet_id ports from to cidr current revoke authorize wanted after
  if tags="$(stack_tags "$stack")"; then :; else
    absent_or_exit "$?"
    die "stack $stack does not exist"
  fi
  tags_belong_to_demo "$tags" || die "stack $stack is not tagged demo-env=$CLUSTER_NAME"
  fleet_id="$(aws_lookup 'NOT_A_MATCH' cloudformation describe-stacks --region "$REGION" --stack-name "$stack" \
    --query "Stacks[0].Outputs[?OutputKey=='FleetId'].OutputValue" --output text)"
  [[ "$fleet_id" =~ ^containerfleet-[0-9a-f-]{36}$ ]] || die "stack $stack has no FleetId output for a container fleet"
  ports="$(aws_lookup 'NOT_A_MATCH' gamelift describe-container-fleet --region "$REGION" --fleet-id "$fleet_id" \
    --query 'ContainerFleet.InstanceConnectionPortRange.[FromPort,ToPort]' --output text)"
  read -r from to <<<"$ports"
  valid_port "$from" && valid_port "$to" && [ "$from" -le "$to" ] || die "unexpected connection port range: $from-$to"
  cidr="$(nat_egress_cidr)"
  wanted="[{\"FromPort\":$from,\"ToPort\":$to,\"IpRange\":\"$cidr\",\"Protocol\":\"UDP\"}]"
  # Revoke every existing permission except the one we want, whatever its
  # range, protocol, or address family; then authorize only the NAT address.
  current="$(aws_lookup 'NOT_A_MATCH' gamelift describe-container-fleet --region "$REGION" --fleet-id "$fleet_id" \
    --query 'ContainerFleet.InstanceInboundPermissions' --output json)"
  revoke="$(jq -c --argjson keep "$wanted" '[ (. // [])[] | select(. as $p | $keep | index($p) | not) ]' <<<"$current")"
  authorize="$(jq -c --argjson want "$wanted" '(. // []) as $have | [ $want[] | select(. as $p | $have | index($p) | not) ]' <<<"$current")"
  if [ "$revoke" = "[]" ] && [ "$authorize" = "[]" ]; then
    log "Fleet $fleet_id already allows only UDP $from-$to from $cidr"
  else
    log "Fleet $fleet_id: allowing UDP $from-$to from $cidr only"
    local -a update=(gamelift update-container-fleet --region "$REGION" --fleet-id "$fleet_id")
    [ "$authorize" = "[]" ] || update+=(--instance-inbound-permission-authorizations "$authorize")
    [ "$revoke" = "[]" ] || update+=(--instance-inbound-permission-revocations "$revoke")
    aws_lookup 'NOT_A_MATCH' "${update[@]}" >/dev/null
  fi
  after="$(aws_lookup 'NOT_A_MATCH' gamelift describe-container-fleet --region "$REGION" --fleet-id "$fleet_id" \
    --query 'ContainerFleet.InstanceInboundPermissions' --output json)"
  jq -e --argjson want "$wanted" '(. // []) | sort == ($want | sort)' >/dev/null <<<"$after" \
    || die "fleet $fleet_id inbound permissions are not exactly UDP $from-$to from $cidr: $after"
}

cmd_load() {
  validate_settings
  local host="${1:-}" port="${2:-}" players="${3:-8}" seconds="${4:-30}"
  valid_ipv4 "$host" || die "usage: load HOST PORT [PLAYERS] [SECONDS]"
  valid_port "$port" || die "invalid port: $port"
  [[ "$players" =~ ^[1-9][0-9]{0,2}$ ]] && [ "$players" -le 100 ] || die "PLAYERS must be 1-100"
  [[ "$seconds" =~ ^[1-9][0-9]{0,3}$ ]] && [ "$seconds" -le 3600 ] || die "SECONDS must be 1-3600"
  use_demo_kube
  kubectl delete job udp-load --ignore-not-found >/dev/null
  sed -e "s|REPLACE_WITH_GAMESERVER_POD_IP|$host|" \
      -e "s|value: \"7654\"|value: \"$port\"|" \
      -e "s|value: \"10\"|value: \"$players\"|" \
      -e "s|value: \"120\"|value: \"$seconds\"|" \
      -e "s|activeDeadlineSeconds: 420|activeDeadlineSeconds: $((seconds + 300))|" \
      "$DEMO_DIR/client/load-job.yaml" | kubectl apply -f -
  kubectl wait --for=condition=complete job/udp-load --timeout="$((seconds + 120))s"
  kubectl logs job/udp-load | tail -n 3
}

wait_for_stack_settled() {
  # Waits until a stack is no longer *_IN_PROGRESS (at most WAIT_MINUTES).
  # REVIEW_IN_PROGRESS (a change set that was never executed) counts as
  # settled: such a stack can be deleted at once.
  local stack="$1" status deadline=$((SECONDS + ${WAIT_MINUTES:-60} * 60))
  while :; do
    if status="$(aws_lookup 'does not exist' cloudformation describe-stacks --region "$REGION" --stack-name "$stack" \
      --query 'Stacks[0].StackStatus' --output text)"; then :; else
      absent_or_exit "$?"
      return 0
    fi
    [[ "$status" == *_IN_PROGRESS && "$status" != REVIEW_IN_PROGRESS ]] || return 0
    [ "$SECONDS" -lt "$deadline" ] || die "stack $stack is still $status"
    log "Waiting for $stack ($status)"
    sleep "${POLL_SECONDS:-30}"
  done
}

confirm_down() {
  local plan="$1" answer
  echo "The following will be deleted in $REGION:"
  echo "$plan"
  if [ "${DEMO_CONFIRM:-}" = "$CLUSTER_NAME" ]; then
    return 0
  fi
  [ -t 0 ] || die "refusing to delete without confirmation; set DEMO_CONFIRM=$CLUSTER_NAME"
  read -r -p "Type the cluster name ($CLUSTER_NAME) to continue: " answer
  [ "$answer" = "$CLUSTER_NAME" ] || die "not confirmed"
}

# Container fleet IDs a stack created, one per line.
stack_fleet_ids() {
  local ids id
  ids="$(aws_lookup 'NOT_A_MATCH' cloudformation describe-stack-resources --region "$REGION" --stack-name "$1" \
    --query "StackResources[?ResourceType=='AWS::GameLift::ContainerFleet'].PhysicalResourceId" --output text)" || exit 1
  for id in $ids; do
    if [[ "$id" =~ ^containerfleet-[0-9a-f-]{36}$ ]]; then echo "$id"; fi
  done
}

# Deletes the default gamelift-<fleet ID>-<Region> log group GameLift creates
# for a fleet without LogConfiguration; it outlives the fleet and its stack.
delete_default_fleet_log_group() {
  local group="gamelift-$1-$REGION"
  if aws_lookup 'ResourceNotFoundException' logs delete-log-group --region "$REGION" --log-group-name "$group" >/dev/null; then
    log "Deleted log group $group"
  fi
}

cmd_down() {
  validate_settings
  require_tools jq
  local stacks name status cluster_present=false eksctl_leftovers="" repo_arn="" tags plan="" fleets="" fleet
  # Resolve exactly what belongs to this environment before deleting anything.
  stacks="$(demo_stacks)"
  while read -r name status; do
    [ -n "$name" ] && plan+="  GameLift stack $name ($status)"$'\n'
  done <<<"$stacks"
  if tags="$(cluster_tags)"; then
    tags_belong_to_demo "$tags" || die "cluster $CLUSTER_NAME exists but is not tagged demo-env=$CLUSTER_NAME; refusing to delete it"
    cluster_present=true
    require_tools eksctl helm kubectl
    plan+="  EKS cluster $CLUSTER_NAME, its node group, VPC, and NAT gateway"$'\n'
  else
    absent_or_exit "$?"
    # A failed cluster-up can leave eksctl stacks behind without a cluster.
    eksctl_leftovers="$(demo_stacks "eksctl-${CLUSTER_NAME}-")"
    if [ -n "$eksctl_leftovers" ]; then
      require_tools eksctl
      while read -r name status; do
        plan+="  eksctl stack $name ($status), left by a failed cluster-up"$'\n'
      done <<<"$eksctl_leftovers"
    fi
  fi
  if repo_arn="$(repository_arn)"; then
    tags="$(repository_tags "$repo_arn")"
    if tags_belong_to_demo "$tags"; then
      plan+="  ECR repository $REPOSITORY_NAME and all of its images"$'\n'
    else
      log "Skipping ECR repository $REPOSITORY_NAME: not tagged demo-env=$CLUSTER_NAME"
      repo_arn=""
    fi
  else
    absent_or_exit "$?"
    repo_arn=""
  fi
  if [ -z "$plan" ]; then
    log "Nothing to delete"
    cmd_verify_clean
    return
  fi
  confirm_down "$plan"

  # 1. GameLift stacks first: fleets bill per instance hour. A container fleet
  #    cannot be deleted while it is still being created, so wait it out.
  while read -r name status; do
    [ -n "$name" ] || continue
    wait_for_stack_settled "$name"
    fleets+="$(stack_fleet_ids "$name")"$'\n'
    log "Deleting GameLift stack $name"
    aws_lookup 'NOT_A_MATCH' cloudformation delete-stack --region "$REGION" --stack-name "$name" >/dev/null
  done <<<"$stacks"
  while read -r name status; do
    [ -n "$name" ] || continue
    aws_lookup 'NOT_A_MATCH' cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$name" >/dev/null \
      || die "stack $name did not delete"
  done <<<"$stacks"
  # 2. Kubernetes workloads, in the demo's own cluster only.
  if [ "$cluster_present" = true ]; then
    use_demo_kube
    if kubectl get ns agones-system >/dev/null 2>&1; then
      log "Removing the fleet and Agones"
      kubectl delete -f "$DEMO_DIR/agones/fleet.yaml" --ignore-not-found --wait=true || true
      helm uninstall agones --namespace agones-system --wait || true
    fi
  fi
  # 3. The cluster, its node group, VPC, and NAT gateway (eksctl stacks), or
  #    the stacks a failed cluster-up left behind.
  if [ "$cluster_present" = true ] || [ -n "$eksctl_leftovers" ]; then
    log "Deleting cluster $CLUSTER_NAME"
    eksctl delete cluster --region "$REGION" --name "$CLUSTER_NAME" --disable-nodegroup-eviction --wait
  fi
  # 4. The image repository.
  if [ -n "$repo_arn" ]; then
    log "Deleting ECR repository $REPOSITORY_NAME"
    aws_lookup 'NOT_A_MATCH' ecr delete-repository --region "$REGION" --repository-name "$REPOSITORY_NAME" --force >/dev/null
  fi
  # 5. Default fleet log groups last: they cost almost nothing, so a denied
  #    delete (for example under an operator role deployed before this step
  #    existed) never leaves the cluster or NAT gateway running.
  for fleet in $fleets; do
    delete_default_fleet_log_group "$fleet"
  done
  rm -f "$DEMO_KUBECONFIG"
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
  # in the same account is neither reported nor touched. A failed tagging call
  # stops the script instead of reading as "nothing tagged".
  local listing arn
  listing="$(aws_lookup 'NOT_A_MATCH' resourcegroupstaggingapi get-resources --region "$REGION" \
    --tag-filters "Key=$DEMO_TAG_KEY,Values=$DEMO_TAG_VALUE" "Key=demo-env,Values=$CLUSTER_NAME" \
    --query 'ResourceTagMappingList[].ResourceARN' --output text)" || exit 1
  for arn in $listing; do
    [ "$arn" != "None" ] || continue
    if [[ "$arn" == arn:*:ec2:* ]] && ! ec2_resource_exists "$arn"; then
      continue
    fi
    echo "$arn"
  done
}

cmd_verify_clean() {
  validate_settings
  require_tools jq
  local remaining=0 found attempt tags
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
  [ -z "$found" ] || { echo "GameLift stacks remain:"; echo "$found"; remaining=1; }
  found="$(stacks_with_prefix "eksctl-${CLUSTER_NAME}-")"
  [ -z "$found" ] || [ "$found" = "None" ] || { echo "eksctl stacks remain:"; echo "$found"; remaining=1; }
  # Fleet log groups the reference template creates (also caught by tag above
  # once the tagging API lists them).
  found="$(aws_lookup 'NOT_A_MATCH' logs describe-log-groups --region "$REGION" \
    --log-group-name-prefix "gamelift-${STACK_PREFIX}-" --query 'logGroups[].logGroupName' --output text)" || exit 1
  [ -z "$found" ] || [ "$found" = "None" ] || { echo "Fleet log groups remain:"; echo "$found"; remaining=1; }
  # The cluster and repository count only if they carry this environment's
  # tags, the same scoping `down` applies before deleting them.
  local repo_arn
  if tags="$(cluster_tags)"; then
    if tags_belong_to_demo "$tags"; then
      echo "Cluster remains: $CLUSTER_NAME"; remaining=1
    else
      log "Cluster $CLUSTER_NAME exists but is not tagged demo-env=$CLUSTER_NAME; not counted"
    fi
  else
    absent_or_exit "$?"
  fi
  if repo_arn="$(repository_arn)"; then
    tags="$(repository_tags "$repo_arn")"
    if tags_belong_to_demo "$tags"; then
      echo "ECR repository remains: $REPOSITORY_NAME"; remaining=1
    else
      log "ECR repository $REPOSITORY_NAME exists but is not tagged demo-env=$CLUSTER_NAME; not counted"
    fi
  else
    absent_or_exit "$?"
  fi
  [ "$remaining" -eq 0 ] || die "demo resources remain (see above)"
  log "No resources of environment $CLUSTER_NAME remain in $REGION"
}

main() {
  local command="${1:-}"
  shift || true
  case "$command" in
    preflight) cmd_preflight ;;
    cluster-up) cmd_cluster_up ;;
    agones-up) cmd_agones_up ;;
    enroll-agent) cmd_enroll_agent ;;
    kubeconfig) cmd_kubeconfig ;;
    nat-cidr) cmd_nat_cidr ;;
    boundary-arn) cmd_boundary_arn ;;
    restrict-fleet-ingress) cmd_restrict_fleet_ingress "$@" ;;
    load) cmd_load "$@" ;;
    down) cmd_down ;;
    verify-clean) cmd_verify_clean ;;
    *) sed -n '2,34p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; [ -z "$command" ] || exit 2 ;;
  esac
}

main "$@"
