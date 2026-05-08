#!/usr/bin/env bash
# =============================================================================
# run_load_test.sh — IaC Drift Detection SaaS Load Test Orchestrator
#
# Run this script from the SaaS terminal.
# It guides you through all 5 rubric-aligned test scenarios and records
# precise timestamps to scenario_log.json for post-run analysis.
#
# Test Scenarios:
#   T1 — Functional   : End-to-end pipeline with direct stack-processor invoke
#   T2 — Scaling      : 10 stacks, 1 drift round (parallel Lambda scale-out)
#   T3 — Performance  : 10 stacks, 3 drift rounds (peak load, P95/P99)
#   T4 — Failure      : Bad-tenant injection + normal drift (error handling)
#   T5 — Security     : CloudWatch log scan for credential leakage
#
# Prerequisites:
#   - SaaS terminal: AWS credentials for the SaaS account (Lambda, DynamoDB,
#                    CloudWatch, SQS access)
#   - Customer terminal: AWS credentials for the customer account (CloudFormation,
#                        S3, IAM, EC2, SQS, Lambda, SNS, CloudWatch access)
#   - customer_iac repo cloned (set CUSTOMER_SCRIPTS_DIR below or via env var)
#
# Usage:
#   bash scripts/load_test/run_load_test.sh [--wait N]
#   WAIT_MINUTES=15 bash scripts/load_test/run_load_test.sh
#   PROJECT=my-project bash scripts/load_test/run_load_test.sh
#
# Output: scenario_log.json in current directory (read by analyze.py)
# =============================================================================

set -uo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
PROJECT="${PROJECT:-drift-detector}"
REGION="${REGION:-us-east-1}"
WAIT_MINUTES="${WAIT_MINUTES:-12}"
SCENARIO_LOG="${SCENARIO_LOG:-scenario_log.json}"

# Path to the customer_iac repo's scripts directory.
# Override via CUSTOMER_SCRIPTS_DIR env var if your layout differs.
CUSTOMER_SCRIPTS_DIR="${CUSTOMER_SCRIPTS_DIR:-../../customer_iac/drift-detector-cfn-templates/scripts}"

PROCESSOR_FN="${PROJECT}-processor"
STACK_PROCESSOR_FN="${PROJECT}-stack-processor"
TENANTS_TABLE="${PROJECT}-tenants"
DLQ_NAME="${PROJECT}-processor-dlq"

# Colours (no-ops if terminal doesn't support them)
RED='\033[0;31m'; GRN='\033[0;32m'; YEL='\033[1;33m'
BLU='\033[0;34m'; CYN='\033[0;36m'; NC='\033[0m'

# ── Output helpers ─────────────────────────────────────────────────────────────
section() { echo; echo -e "${BLU}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; echo -e "${BLU}  $*${NC}"; echo -e "${BLU}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"; echo; }
info()    { echo -e "  ${GRN}[INFO]${NC} $*"; }
warn()    { echo -e "  ${YEL}[WARN]${NC} $*"; }
err()     { echo -e "  ${RED}[ERR ]${NC} $*"; }
step()    { echo -e "  ${CYN}[STEP]${NC} $*"; }
customer(){ echo; echo -e "  ${YEL}┌─ CUSTOMER TERMINAL ─────────────────────────────────────────┐${NC}"; echo -e "  ${YEL}│${NC}  $*"; echo -e "  ${YEL}└──────────────────────────────────────────────────────────────┘${NC}"; echo; }

pause() {
  local msg="${1:-Press Enter to continue...}"
  echo -e "  ${CYN}[WAIT]${NC} ${msg}"
  read -r
}

iso_now() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

# ── scenario_log.json management ──────────────────────────────────────────────
# Accumulate entries in a bash array, flush to file after each scenario.
declare -a LOG_ENTRIES=()

# Usage: add_entry <json-object-string>
add_entry() { LOG_ENTRIES+=("$1"); }

flush_log() {
  printf '[' > "$SCENARIO_LOG"
  local i=0
  for entry in "${LOG_ENTRIES[@]}"; do
    [[ $i -gt 0 ]] && printf ',' >> "$SCENARIO_LOG"
    printf '%s' "$entry" >> "$SCENARIO_LOG"
    i=$(( i + 1 ))
  done
  printf ']' >> "$SCENARIO_LOG"
  info "scenario_log.json updated (${#LOG_ENTRIES[@]} scenario(s) recorded)"
}

# ── AWS helpers ───────────────────────────────────────────────────────────────
invoke_processor() {
  step "Invoking ${PROCESSOR_FN} Lambda (async)..."
  local out
  out=$(aws lambda invoke \
    --region "$REGION" \
    --function-name "$PROCESSOR_FN" \
    --invocation-type Event \
    --payload '{}' \
    /dev/null 2>&1) && info "Processor Lambda invoked (async)" \
                     || warn "Invoke returned: $out"
}

wait_for_processing() {
  local minutes="${1:-$WAIT_MINUTES}"
  info "Waiting ${minutes} minutes for Lambda pipeline to complete..."
  info "  (stack_processor × N → validator → pr_creator)"
  local i
  for (( i=minutes; i>0; i-- )); do
    printf "  ${CYN}  %2d min remaining...${NC}\r" "$i"
    sleep 60
  done
  echo
  info "Wait complete."
}

inject_bad_tenant() {
  step "Injecting invalid tenant record for fault injection test..."
  aws dynamodb put-item \
    --region "$REGION" \
    --table-name "$TENANTS_TABLE" \
    --item '{
      "tenant_id":         {"S": "load-test-bad-tenant"},
      "role_arn":          {"S": "arn:aws:iam::000000000000:role/NonExistentCrossAccountRole"},
      "external_id":       {"S": "invalid-external-id-00000000"},
      "github_repo":       {"S": "owner/nonexistent-repo"},
      "cloudtrail_bucket": {"S": "nonexistent-cloudtrail-bucket-xyz"}
    }' 2>/dev/null \
    && info "Bad tenant 'load-test-bad-tenant' injected into ${TENANTS_TABLE}" \
    || warn "Failed to inject bad tenant — T4 failure test may be incomplete"
}

remove_bad_tenant() {
  step "Removing bad tenant record..."
  aws dynamodb delete-item \
    --region "$REGION" \
    --table-name "$TENANTS_TABLE" \
    --key '{"tenant_id": {"S": "load-test-bad-tenant"}}' 2>/dev/null \
    && info "Bad tenant record removed" \
    || warn "Could not remove bad tenant — please delete manually from DynamoDB"
}

get_tenant_info() {
  # Returns the first tenant item as compact JSON for direct stack-processor invocation (T1)
  aws dynamodb scan \
    --region "$REGION" \
    --table-name "$TENANTS_TABLE" \
    --max-items 1 \
    --query 'Items[0]' \
    --output json 2>/dev/null || echo "null"
}

flatten_ddb_item() {
  # Convert DynamoDB attribute-typed JSON to plain JSON via Python
  python3 -c "
import json, sys
raw = json.load(sys.stdin)
if raw is None:
    print('null')
    sys.exit(0)
out = {}
for k, v in raw.items():
    if 'S' in v:  out[k] = v['S']
    elif 'N' in v: out[k] = v['N']
    elif 'BOOL' in v: out[k] = v['BOOL']
    else: out[k] = str(v)
print(json.dumps(out))
"
}

invoke_stack_processor_direct() {
  # T1: invoke stack-processor directly with a synthetic event for one stack
  local tenant_json="$1"
  local stack_name="$2"
  local event_id="functional-test-$(date +%s)"

  step "Invoking ${STACK_PROCESSOR_FN} directly (synthetic event for ${stack_name})..."

  local payload
  payload=$(python3 -c "
import json, sys
tenant = json.loads(sys.argv[1])
stack  = sys.argv[2]
eid    = sys.argv[3]
payload = {
  'tenant': tenant,
  'stack_name': stack,
  'cloudtrail_events': [{
    'eventID':            eid,
    'eventName':          'PutBucketTagging',
    'awsRegion':          'us-east-1',
    'eventSource':        's3.amazonaws.com',
    'eventTime':          '$(date -u +%Y-%m-%dT%H:%M:%SZ)',
    'userAgent':          'load-test/1.0',
    'requestParameters':  {'bucketName': 'drift-test-01-bucket'},
    'responseElements':   None,
    'readOnly':           False
  }]
}
print(json.dumps(payload))
" "$tenant_json" "$stack_name" "$event_id")

  aws lambda invoke \
    --region "$REGION" \
    --function-name "$STACK_PROCESSOR_FN" \
    --invocation-type Event \
    --payload "$payload" \
    /dev/null 2>/dev/null \
    && info "stack-processor invoked directly for ${stack_name}" \
    || warn "Failed to invoke stack-processor directly — check Lambda name / permissions"
}

run_security_scan() {
  step "Scanning CloudWatch logs for credential leakage..."
  local log_group="/aws/lambda/${STACK_PROCESSOR_FN}"
  local start_ms=$(( ($(date +%s) - 86400) * 1000 ))  # last 24h

  local patterns=("AKIA" "ghp_" "password" "secret_key" "aws_secret")
  local found=0

  for pattern in "${patterns[@]}"; do
    local count
    count=$(aws logs filter-log-events \
      --region "$REGION" \
      --log-group-name "$log_group" \
      --start-time "$start_ms" \
      --filter-pattern "\"${pattern}\"" \
      --query 'length(events)' \
      --output text 2>/dev/null || echo "0")
    count="${count:-0}"
    if [[ "$count" -gt 0 ]]; then
      err "SECURITY: Pattern '${pattern}' found ${count} time(s) in logs — INVESTIGATE"
      found=$(( found + 1 ))
    else
      info "  Pattern '${pattern}': NOT FOUND in logs (expected)"
    fi
  done

  # Verify cross-account role assumption IS logged (functional check)
  local role_count
  role_count=$(aws logs filter-log-events \
    --region "$REGION" \
    --log-group-name "/aws/lambda/${PROCESSOR_FN}" \
    --start-time "$start_ms" \
    --filter-pattern '"Assumed role"' \
    --query 'length(events)' \
    --output text 2>/dev/null || echo "0")

  if [[ "${role_count:-0}" -gt 0 ]]; then
    info "  Cross-account role assumption logged ${role_count} time(s) (expected)"
  else
    warn "  Cross-account role assumption not found in processor logs — check tenant config"
  fi

  if [[ $found -eq 0 ]]; then
    info "Security scan PASSED — no credential patterns found in logs"
    echo "passed"
  else
    warn "Security scan FLAGGED ${found} pattern(s) — review above"
    echo "flagged:${found}"
  fi
}

# ── Scenario definitions ───────────────────────────────────────────────────────

scenario_t1() {
  section "T1 — Functional: End-to-End Pipeline Verification"
  info "Objective: Verify the complete drift detection pipeline for a single stack."
  info "Method:    Invoke stack-processor directly with a synthetic CloudTrail event."
  info "Expected:  stack-processor calls Bedrock → validator → pr-creator → PR opened."
  echo

  local tenant_raw tenant_json
  tenant_raw=$(get_tenant_info)
  if [[ "$tenant_raw" == "null" || -z "$tenant_raw" ]]; then
    err "No tenant records found in ${TENANTS_TABLE}. Cannot run T1."
    err "Ensure the customer tenant is onboarded before running load tests."
    return 1
  fi
  tenant_json=$(echo "$tenant_raw" | flatten_ddb_item)
  info "Using tenant: $(echo "$tenant_json" | python3 -c 'import json,sys; t=json.load(sys.stdin); print(t.get("tenant_id","?"))')"

  local drift_at
  drift_at=$(iso_now)
  invoke_stack_processor_direct "$tenant_json" "drift-test-01"

  local processor_at
  processor_at=$(iso_now)
  local analysis_start="$processor_at"

  local wait_t1=10
  wait_for_processing "$wait_t1"
  local analysis_end
  analysis_end=$(iso_now)

  add_entry "{
    \"id\": \"T1\",
    \"name\": \"T1-Functional\",
    \"type\": \"functional\",
    \"description\": \"Direct stack-processor invocation with synthetic CloudTrail event for drift-test-01. Verifies LLM call, validator, and PR creation end-to-end.\",
    \"objective\": \"Verify the complete drift detection pipeline for a single stack without CloudTrail ingestion overhead\",
    \"expected_outcome\": \"stack-processor invokes Bedrock (latency_ms logged), validator accepts template, pr-creator opens a GitHub PR\",
    \"stacks\": 1,
    \"drift_rounds\": [0],
    \"drift_induced_at\": \"${drift_at}\",
    \"processor_invoked_at\": \"${processor_at}\",
    \"analysis_start\": \"${analysis_start}\",
    \"analysis_end\": \"${analysis_end}\"
  }"
  flush_log
  info "T1 complete."
}

scenario_t2() {
  section "T2 — Scaling: Horizontal Lambda Scale-Out (10 Stacks, Round 1)"
  info "Objective: Verify Lambda scales to 10 concurrent invocations under parallel stack load."
  info "Method:    Induce drift on all 10 stacks (round 1), invoke orchestrator processor."
  info "Expected:  10 parallel stack-processor invocations; ConcurrentExecutions peaks at ~10."
  echo

  customer "On your CUSTOMER terminal, run:"
  echo -e "    ${YEL}cd /path/to/customer_iac/drift-detector-cfn-templates${NC}"
  echo -e "    ${YEL}bash scripts/drift-inducer.sh 1${NC}"
  echo
  pause "Press Enter once drift-inducer.sh has COMPLETED on the customer terminal..."

  local drift_at
  drift_at=$(iso_now)

  step "Triggering orchestrator..."
  invoke_processor
  local processor_at
  processor_at=$(iso_now)
  local analysis_start="$processor_at"

  wait_for_processing "$WAIT_MINUTES"
  local analysis_end
  analysis_end=$(iso_now)

  add_entry "{
    \"id\": \"T2\",
    \"name\": \"T2-Scaling\",
    \"type\": \"scaling\",
    \"description\": \"10 stacks drifted (round 1), orchestrator processes all in parallel. Measures Lambda horizontal scale-out and throughput at moderate load.\",
    \"objective\": \"Verify Lambda concurrency scales to handle 10 parallel stacks and measure throughput baseline\",
    \"expected_outcome\": \"10 parallel stack-processor invocations, peak ConcurrentExecutions = 10, no throttles\",
    \"stacks\": 10,
    \"drift_rounds\": [1],
    \"drift_induced_at\": \"${drift_at}\",
    \"processor_invoked_at\": \"${processor_at}\",
    \"analysis_start\": \"${analysis_start}\",
    \"analysis_end\": \"${analysis_end}\"
  }"
  flush_log

  customer "Reset stacks on your CUSTOMER terminal:"
  echo -e "    ${YEL}bash scripts/reset-stacks.sh${NC}"
  echo
  pause "Press Enter once reset-stacks.sh has COMPLETED..."
  info "T2 complete."
}

scenario_t3() {
  section "T3 — Performance: Peak Load (10 Stacks, All 3 Drift Rounds)"
  info "Objective: Measure P95/P99 latency and max throughput under maximum drift complexity."
  info "Method:    Induce 3 rounds of drift (each increasing in complexity) on all 10 stacks,"
  info "           then invoke the orchestrator. Processor sees highest event density per stack."
  info "Expected:  Bedrock P99 measurable; no crashes; throughput >= T2 baseline."
  echo

  customer "On your CUSTOMER terminal, run ALL 3 rounds back-to-back:"
  echo -e "    ${YEL}bash scripts/drift-inducer.sh 1${NC}"
  echo -e "    ${YEL}bash scripts/drift-inducer.sh 2${NC}"
  echo -e "    ${YEL}bash scripts/drift-inducer.sh 3${NC}"
  echo
  pause "Press Enter once all 3 rounds have COMPLETED on the customer terminal..."

  local drift_at
  drift_at=$(iso_now)

  step "Triggering orchestrator for peak load run..."
  invoke_processor
  local processor_at
  processor_at=$(iso_now)
  local analysis_start="$processor_at"

  local wait_t3=$(( WAIT_MINUTES + 5 ))
  wait_for_processing "$wait_t3"
  local analysis_end
  analysis_end=$(iso_now)

  add_entry "{
    \"id\": \"T3\",
    \"name\": \"T3-Performance\",
    \"type\": \"performance\",
    \"description\": \"10 stacks drifted across all 3 rounds (increasing complexity), then processed together. Highest event density per stack — primary P95/P99 benchmark scenario.\",
    \"objective\": \"Measure P95/P99 Bedrock latency and peak throughput under maximum drift event density\",
    \"expected_outcome\": \"Bedrock P99 < 120000 ms, throughput >= T2, no Lambda crashes or DLQ messages from valid tenants\",
    \"stacks\": 10,
    \"drift_rounds\": [1, 2, 3],
    \"drift_induced_at\": \"${drift_at}\",
    \"processor_invoked_at\": \"${processor_at}\",
    \"analysis_start\": \"${analysis_start}\",
    \"analysis_end\": \"${analysis_end}\"
  }"
  flush_log

  customer "Reset stacks on your CUSTOMER terminal:"
  echo -e "    ${YEL}bash scripts/reset-stacks.sh${NC}"
  echo
  pause "Press Enter once reset-stacks.sh has COMPLETED..."
  info "T3 complete."
}

scenario_t4() {
  section "T4 — Failure: Fault Injection and Graceful Error Handling"
  info "Objective: Verify the system handles invalid tenants gracefully without crashing."
  info "Method:    Inject a DynamoDB tenant record with an invalid cross-account role ARN,"
  info "           induce drift on all 10 stacks, invoke orchestrator. The bad tenant causes"
  info "           STS AssumeRole to fail; valid tenants must still be processed successfully."
  info "Expected:  ERROR logged for bad tenant, valid tenant processing unaffected, DLQ = 0."
  echo

  inject_bad_tenant

  customer "On your CUSTOMER terminal, run drift for round 1:"
  echo -e "    ${YEL}bash scripts/drift-inducer.sh 1${NC}"
  echo
  pause "Press Enter once drift-inducer.sh has COMPLETED on the customer terminal..."

  local drift_at
  drift_at=$(iso_now)

  step "Triggering orchestrator (bad tenant will cause a logged error)..."
  invoke_processor
  local processor_at
  processor_at=$(iso_now)
  local analysis_start="$processor_at"

  wait_for_processing "$WAIT_MINUTES"
  local analysis_end
  analysis_end=$(iso_now)

  remove_bad_tenant

  add_entry "{
    \"id\": \"T4\",
    \"name\": \"T4-Failure\",
    \"type\": \"failure\",
    \"description\": \"Bad tenant with non-existent IAM role injected into DynamoDB. Orchestrator logs ERROR for that tenant and continues processing valid tenants. DLQ should remain empty (no Lambda crashes).\",
    \"objective\": \"Verify graceful per-tenant error isolation: one bad tenant must not block or crash processing of valid tenants\",
    \"expected_outcome\": \"ERROR log for 'load-test-bad-tenant', valid tenant stacks processed normally, DLQ depth = 0, Lambda invocation count unchanged\",
    \"stacks\": 10,
    \"drift_rounds\": [1],
    \"fault\": \"Invalid role_arn in DynamoDB tenant record causes STS AssumeRole ClientError\",
    \"drift_induced_at\": \"${drift_at}\",
    \"processor_invoked_at\": \"${processor_at}\",
    \"analysis_start\": \"${analysis_start}\",
    \"analysis_end\": \"${analysis_end}\"
  }"
  flush_log

  customer "Reset stacks on your CUSTOMER terminal:"
  echo -e "    ${YEL}bash scripts/reset-stacks.sh${NC}"
  echo
  pause "Press Enter once reset-stacks.sh has COMPLETED..."
  info "T4 complete."
}

scenario_t5() {
  section "T5 — Security: CloudWatch Log Credential Leakage Audit"
  info "Objective: Verify no secrets (AWS keys, GitHub tokens, passwords) appear in CloudWatch logs."
  info "Method:    Search the last 24 hours of stack-processor and processor Lambda log streams"
  info "           for known credential patterns. Also verify cross-account role assumption IS"
  info "           logged (confirming IAM flow works and role ARN is visible but not the creds)."
  info "Expected:  Zero matches for secret patterns; role assumption event visible in logs."
  echo

  local drift_at
  drift_at=$(iso_now)

  # Minimal drift to ensure there are fresh log entries to scan
  customer "Run a light single-round drift on your CUSTOMER terminal (for fresh log data):"
  echo -e "    ${YEL}bash scripts/drift-inducer.sh 1${NC}"
  echo
  pause "Press Enter once drift-inducer.sh has COMPLETED..."

  invoke_processor
  local processor_at
  processor_at=$(iso_now)
  local analysis_start="$processor_at"

  local wait_t5=8
  wait_for_processing "$wait_t5"

  step "Running security scan on CloudWatch logs..."
  local scan_result
  scan_result=$(run_security_scan)
  local analysis_end
  analysis_end=$(iso_now)

  # Strip ANSI escape codes and control chars before embedding in JSON
  local scan_clean
  scan_clean=$(echo "$scan_result" | sed 's/\x1b\[[0-9;]*m//g' | tr -d '\r\n\x01-\x08\x0b\x0c\x0e-\x1f\x7f')

  add_entry "{
    \"id\": \"T5\",
    \"name\": \"T5-Security\",
    \"type\": \"security\",
    \"description\": \"CloudWatch log audit for credential leakage. Searches for AWS access key patterns (AKIA), GitHub tokens (ghp_), and password keywords. Also verifies cross-account role assumption is logged correctly.\",
    \"objective\": \"Confirm no secrets are logged to CloudWatch; verify IAM cross-account flow is auditable\",
    \"expected_outcome\": \"Zero occurrences of AKIA / ghp_ / password in logs; role ARN visible in processor logs\",
    \"stacks\": 10,
    \"drift_rounds\": [1],
    \"security_scan_result\": \"${scan_clean}\",
    \"drift_induced_at\": \"${drift_at}\",
    \"processor_invoked_at\": \"${processor_at}\",
    \"analysis_start\": \"${analysis_start}\",
    \"analysis_end\": \"${analysis_end}\"
  }"
  flush_log
  info "T5 complete."
}

# ── Entrypoint ─────────────────────────────────────────────────────────────────

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --wait|-w) WAIT_MINUTES="$2"; shift 2 ;;
      --project) PROJECT="$2"; PROCESSOR_FN="${PROJECT}-processor"; STACK_PROCESSOR_FN="${PROJECT}-stack-processor"; TENANTS_TABLE="${PROJECT}-tenants"; DLQ_NAME="${PROJECT}-processor-dlq"; shift 2 ;;
      --region)  REGION="$2"; shift 2 ;;
      --help|-h) grep '^#' "$0" | head -30 | sed 's/^# \?//'; exit 0 ;;
      *) err "Unknown argument: $1"; exit 1 ;;
    esac
  done
}

main() {
  parse_args "$@"

  echo
  echo -e "${BLU}╔══════════════════════════════════════════════════════════════╗${NC}"
  echo -e "${BLU}║     IaC Drift Detection SaaS — Load Test Orchestrator        ║${NC}"
  echo -e "${BLU}╚══════════════════════════════════════════════════════════════╝${NC}"
  echo
  info "Project      : ${PROJECT}"
  info "Region       : ${REGION}"
  info "Wait per run : ${WAIT_MINUTES} minutes"
  info "Scenario log : ${SCENARIO_LOG}"
  echo
  warn "This script runs on the SaaS terminal."
  warn "You will be prompted at each step to run commands on the customer terminal."
  echo
  pause "Press Enter to start the load test suite (T1 → T5)..."

  # Initialise empty log file
  echo '[]' > "$SCENARIO_LOG"

  scenario_t1 || warn "T1 failed — continuing"
  scenario_t2 || warn "T2 failed — continuing"
  scenario_t3 || warn "T3 failed — continuing"
  scenario_t4 || warn "T4 failed — continuing"
  scenario_t5 || warn "T5 failed — continuing"

  section "All Scenarios Complete"
  info "scenario_log.json written: ${SCENARIO_LOG}"
  info ""
  info "Next step — run the analysis script on this SaaS terminal:"
  echo
  echo -e "    ${YEL}pip install -r scripts/load_test/requirements.txt${NC}"
  echo -e "    ${YEL}python3 scripts/load_test/analyze.py --scenario-log ${SCENARIO_LOG} --output results/${NC}"
  echo
  info "This will generate 7 graphs (results/G1_*.png … results/G7_*.png)"
  info "and a full rubric-aligned report (results/report.md)."
  echo
}

main "$@"
