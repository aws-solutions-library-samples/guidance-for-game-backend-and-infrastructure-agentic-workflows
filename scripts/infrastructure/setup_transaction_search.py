#!/usr/bin/env python3
"""
Setup CloudWatch Transaction Search (Account-Wide, One-Time)

This script idempotently ensures Transaction Search is properly configured,
including the internal 'aws/spans' log group that X-Ray needs.

BACKGROUND: The AgentCore CLI enables Transaction Search via API during
`agentcore launch`, but the API path does NOT create the internal 'aws/spans'
log group. This log group (in the AWS-reserved namespace) is only created when
Transaction Search is toggled (disabled -> re-enabled) or enabled via Console.

See: https://github.com/aws/bedrock-agentcore-starter-toolkit/issues/457
"""

import os
import sys
import json
import time
import boto3

AWS_REGION = os.environ.get('AWS_REGION', os.environ.get('AWS_DEFAULT_REGION', 'us-west-2'))


def _aws_spans_log_group_exists(logs):
    """Check if the 'aws/spans' log group exists (AWS-reserved namespace, no leading /)."""
    try:
        response = logs.describe_log_groups(logGroupNamePrefix='aws/spans')
        for lg in response.get('logGroups', []):
            if lg['logGroupName'] == 'aws/spans':
                return True
        return False
    except Exception:
        return False


def _wait_for_active(xray, timeout=300):
    """Wait for Transaction Search to reach ACTIVE status."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            response = xray.get_trace_segment_destination()
            if response.get('Status') == 'ACTIVE':
                return response.get('Destination')
        except Exception:
            pass
        time.sleep(10)
    return None


def enable_transaction_search():
    """Enable Transaction Search with proper log group creation."""

    print("🔍 Checking Transaction Search status...")

    sts = boto3.client('sts', region_name=AWS_REGION)
    account_id = sts.get_caller_identity()['Account']

    logs = boto3.client('logs', region_name=AWS_REGION)
    xray = boto3.client('xray', region_name=AWS_REGION)

    # Step 1: Ensure 'aws/spans' log group exists
    # X-Ray writes spans to log group 'aws/spans' (WITHOUT leading /) with
    # log stream 'default'. This is in the AWS-reserved namespace — only AWS
    # can create it. Toggle Transaction Search to trigger creation.
    print("  📦 Checking aws/spans log group...")
    if _aws_spans_log_group_exists(logs):
        print("  ✅ aws/spans log group exists")
    else:
        print("  ⚠️  aws/spans log group missing — toggling Transaction Search...")

        try:
            current = xray.get_trace_segment_destination()
            current_dest = current.get('Destination', 'UNKNOWN')
        except Exception:
            current_dest = 'UNKNOWN'

        if current_dest == 'CloudWatchLogs':
            # Toggle: disable → wait → re-enable
            print("  🔄 Disabling Transaction Search temporarily...")
            xray.update_trace_segment_destination(Destination='XRay')
            _wait_for_active(xray)

            print("  🔄 Re-enabling Transaction Search...")
            xray.update_trace_segment_destination(Destination='CloudWatchLogs')
        else:
            print("  🎯 Enabling Transaction Search...")
            xray.update_trace_segment_destination(Destination='CloudWatchLogs')

        print("  ⏳ Waiting for aws/spans log group creation...")
        lg_created = False
        start = time.time()
        while time.time() - start < 300:
            dest = _wait_for_active(xray, timeout=30)
            if dest == 'CloudWatchLogs' and _aws_spans_log_group_exists(logs):
                print("  ✅ aws/spans log group created")
                lg_created = True
                break
            time.sleep(10)

        # Fallback: if first-time enable didn't create log group, force a toggle
        if not lg_created:
            print("  ⚠️  Log group not created by initial enable — forcing toggle...")
            try:
                xray.update_trace_segment_destination(Destination='XRay')
                _wait_for_active(xray)
                xray.update_trace_segment_destination(Destination='CloudWatchLogs')
                start = time.time()
                while time.time() - start < 300:
                    dest = _wait_for_active(xray, timeout=30)
                    if dest == 'CloudWatchLogs' and _aws_spans_log_group_exists(logs):
                        print("  ✅ aws/spans log group created (via toggle)")
                        lg_created = True
                        break
                    time.sleep(10)
            except Exception as e:
                print(f"  ⚠️  Toggle failed: {e}")

            if not lg_created:
                print("  ⚠️  WARNING: aws/spans log group could not be created. OTEL trace export may fail.")
                print("  ⚠️  Try enabling Transaction Search via the AWS Console as a fallback.")

    # Step 2: CloudWatch Logs resource policy
    # X-Ray's internal role needs PutLogEvents on 'aws/spans' (no leading /).
    print("  📝 Configuring CloudWatch Logs resource policy...")
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "TransactionSearchXRayAccess",
            "Effect": "Allow",
            "Principal": {"Service": "xray.amazonaws.com"},
            "Action": "logs:PutLogEvents",
            "Resource": [
                f"arn:aws:logs:{AWS_REGION}:{account_id}:log-group:aws/spans:*",
                f"arn:aws:logs:{AWS_REGION}:{account_id}:log-group:/aws/application-signals/data:*"
            ],
            "Condition": {
                "ArnLike": {"aws:SourceArn": f"arn:aws:xray:{AWS_REGION}:{account_id}:*"},
                "StringEquals": {"aws:SourceAccount": account_id}
            }
        }]
    }

    try:
        logs.put_resource_policy(
            policyName='TransactionSearchXRayAccess',
            policyDocument=json.dumps(policy)
        )
        print("  ✅ CloudWatch Logs resource policy configured")
    except Exception as e:
        if 'LimitExceededException' in str(e) or 'already exists' in str(e).lower():
            print("  ✅ CloudWatch Logs resource policy already exists")
        else:
            print(f"  ⚠️  Warning: {e}")

    # Step 3: Ensure destination is CloudWatch Logs
    try:
        response = xray.get_trace_segment_destination()
        if response.get('Destination') != 'CloudWatchLogs':
            xray.update_trace_segment_destination(Destination='CloudWatchLogs')
            print("  ✅ X-Ray destination set to CloudWatch Logs")
        else:
            print("  ✅ X-Ray destination already set to CloudWatch Logs")
    except Exception as e:
        print(f"  ⚠️  Warning: {e}")

    # Step 4: Set sampling to 1% (free tier)
    try:
        xray.update_indexing_rule(
            Name='Default',
            Rule={'Probabilistic': {'DesiredSamplingPercentage': 1}}
        )
        print("  ✅ X-Ray sampling set to 1% (free tier)")
    except Exception as e:
        print(f"  ⚠️  Warning: {e}")

    print("")
    print("✅ Transaction Search configured successfully")
    print("ℹ️  X-Ray spans flow to log group 'aws/spans' (stream: default)")
    return True


if __name__ == "__main__":
    try:
        enable_transaction_search()
    except Exception as e:
        print(f"\n❌ Failed to configure Transaction Search: {e}")
        sys.exit(1)
