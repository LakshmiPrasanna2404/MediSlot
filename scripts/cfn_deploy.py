#!/usr/bin/env python3
"""Deploy one CloudFormation stack through a change set.

    python scripts/cfn_deploy.py --stack medislot-dev-network \
        --template infrastructure/network-stack.yaml \
        --param Environment=dev --param S3PrefixListId=pl-123 \
        --bucket <artifacts bucket> \
        [--import RefreshTokensTable --import DoctorProfileFilesBucket]

1. Uploads the template to S3 (templates over 51,200 bytes must come from S3).
2. --import: resources that already exist in the account but not in the stack
   (created by hand before they were in a template) are first brought under
   CloudFormation with an IMPORT change set. Runs once; afterwards it is a no-op.
3. Creates a change set, prints every change (action, resource, replacement)
   to the run log, executes it and waits. A change set with no changes is
   deleted and the step passes.

CloudFormation change sets use the GitHub Actions assumed role credentials.\nEvery stack update goes through a change set; nothing is changed directly.
"""

import argparse
import hashlib
import json
import sys
import time

import boto3
from botocore.exceptions import ClientError, WaiterError

CAPABILITIES = ["CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"]
NO_CHANGE_REASONS = ("didn't contain changes", "No updates are to be performed")
IMPORTABLE = {
    "AWS::DynamoDB::Table": "TableName",
    "AWS::S3::Bucket": "BucketName",
    "AWS::RDS::DBInstance": "DBInstanceIdentifier",
}


def key_values(items):
    result = {}
    for item in items or []:
        key, _, value = item.partition("=")
        result[key] = value
    return result


def stack_status(cfn, stack):
    try:
        return cfn.describe_stacks(StackName=stack)["Stacks"][0]["StackStatus"]
    except ClientError as exc:
        if "does not exist" in str(exc):
            return None
        raise


def upload_template(bucket, stack, body, region):
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
    key = f"medislot/templates/{stack}/{digest}.template"
    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=body.encode("utf-8"))
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def wait_for_change_set(cfn, stack, name):
    while True:
        result = cfn.describe_change_set(StackName=stack, ChangeSetName=name)
        if result["Status"] in ("CREATE_COMPLETE", "FAILED"):
            return result
        time.sleep(3)


def print_changes(change_set):
    changes = change_set.get("Changes", [])
    print(f"Change set {change_set['ChangeSetName']}: {len(changes)} change(s)")
    print(f"  {'Action':<8} {'Logical ID':<45} {'Type':<38} Replacement")
    for change in changes:
        rc = change["ResourceChange"]
        print(f"  {rc['Action']:<8} {rc['LogicalResourceId']:<45} {rc['ResourceType']:<38} {rc.get('Replacement', '-')}")


def print_failures(cfn, stack):
    events = cfn.describe_stack_events(StackName=stack)["StackEvents"][:40]
    for event in reversed(events):
        if "FAILED" in event["ResourceStatus"]:
            print(f"::error::{event['LogicalResourceId']} {event['ResourceStatus']}: {event.get('ResourceStatusReason', '')}")


def execute(cfn, stack, change_set, change_set_type):
    cfn.execute_change_set(StackName=stack, ChangeSetName=change_set["ChangeSetName"])
    waiter = {
        "CREATE": "stack_create_complete",
        "UPDATE": "stack_update_complete",
        "IMPORT": "stack_import_complete",
    }[change_set_type]
    try:
        cfn.get_waiter(waiter).wait(StackName=stack, WaiterConfig={"Delay": 10, "MaxAttempts": 360})
    except WaiterError:
        print_failures(cfn, stack)
        raise SystemExit(f"{stack}: {change_set_type} failed ({stack_status(cfn, stack)})") from None


def make_change_set(cfn, stack, change_set_type, url, parameters, tags, resources_to_import=None):
    name = f"{change_set_type.lower()}-{int(time.time())}"
    kwargs = {
        "StackName": stack,
        "ChangeSetName": name,
        "ChangeSetType": change_set_type,
        "TemplateURL": url,
        "Parameters": parameters,
        "Capabilities": CAPABILITIES,
        "Tags": [{"Key": k, "Value": v} for k, v in tags.items()],
    }
    if resources_to_import:
        kwargs["ResourcesToImport"] = resources_to_import
    cfn.create_change_set(**kwargs)
    return wait_for_change_set(cfn, stack, name)


def cfn_json(text_or_dict):
    if isinstance(text_or_dict, dict):
        return text_or_dict
    from cfn_flip import to_json

    return json.loads(to_json(text_or_dict))


def resolve_identifier(value, parameters, account, region):
    if isinstance(value, str):
        return value
    if "Fn::Sub" in value and isinstance(value["Fn::Sub"], str):
        text = value["Fn::Sub"]
        for key, val in {**parameters, "AWS::AccountId": account, "AWS::Region": region}.items():
            text = text.replace("${" + key + "}", val)
        return text
    raise SystemExit(f"Cannot resolve resource identifier {value}")


def physical_exists(resource_type, identifier):
    try:
        if resource_type == "AWS::DynamoDB::Table":
            boto3.client("dynamodb").describe_table(TableName=identifier)
        elif resource_type == "AWS::RDS::DBInstance":
            boto3.client("rds").describe_db_instances(DBInstanceIdentifier=identifier)
        else:
            boto3.client("s3").head_bucket(Bucket=identifier)
        return True
    except ClientError:
        return False


def import_existing(cfn, stack, new_template, logical_ids, parameters, bucket, region, tags):
    account = boto3.client("sts").get_caller_identity()["Account"]
    in_stack = set()
    for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack):
        in_stack.update(r["LogicalResourceId"] for r in page["StackResourceSummaries"])
    current = cfn_json(cfn.get_template(StackName=stack, TemplateStage="Original")["TemplateBody"])
    to_import = []
    for logical_id in logical_ids:
        if logical_id in in_stack:
            continue
        resource = new_template["Resources"][logical_id]
        identifier_key = IMPORTABLE[resource["Type"]]
        identifier = resolve_identifier(resource["Properties"][identifier_key], parameters, account, region)
        if physical_exists(resource["Type"], identifier):
            current["Resources"][logical_id] = resource
            to_import.append(
                {"ResourceType": resource["Type"], "LogicalResourceId": logical_id,
                 "ResourceIdentifier": {identifier_key: identifier}}
            )
    if not to_import:
        return
    print(f"Importing {len(to_import)} existing resource(s) into {stack}:")
    for item in to_import:
        print(f"  {item['LogicalResourceId']} <- {item['ResourceIdentifier']}")
    body = json.dumps(current)
    url = upload_template(bucket, stack + "-import", body, region)
    previous = [{"ParameterKey": key, "UsePreviousValue": True} for key in current.get("Parameters", {})]
    change_set = make_change_set(cfn, stack, "IMPORT", url, previous, tags, to_import)
    if change_set["Status"] == "FAILED":
        raise SystemExit(f"Import change set failed: {change_set.get('StatusReason')}")
    print_changes(change_set)
    execute(cfn, stack, change_set, "IMPORT")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", required=True)
    parser.add_argument("--template", required=True)
    parser.add_argument("--param", action="append", default=[])
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--bucket", required=True, help="bucket for the uploaded template")
    parser.add_argument("--import", dest="imports", action="append", default=[],
                        help="logical ID of a resource that may already exist outside the stack")
    args = parser.parse_args()

    session = boto3.session.Session()
    region = session.region_name
    cfn = boto3.client("cloudformation")
    body = open(args.template, encoding="utf-8").read()
    parameters = key_values(args.param)
    tags = {"Project": "MediSlot", **key_values(args.tag)}

    status = stack_status(cfn, args.stack)
    if status == "ROLLBACK_COMPLETE":
        # A failed first create leaves an empty stack that must be cleaned up
        # before retrying. Do not attempt to update it.
        raise SystemExit(
            f"{args.stack} is ROLLBACK_COMPLETE after a failed create. Read the failure in the stack events, "
            f"Delete the empty failed stack, then re-run the GitHub Actions workflow."
        )
    if status == "REVIEW_IN_PROGRESS":
        # A CREATE change set was made but never executed (it failed); the stack
        # has no resources yet, so start again with a new CREATE change set.
        status = None
    if status and status.endswith("_IN_PROGRESS"):
        raise SystemExit(f"{args.stack} is busy ({status}); try again when it finishes")

    if args.imports and status:
        import_existing(cfn, args.stack, cfn_json(body), args.imports, parameters,
                        args.bucket, region, tags)

    change_set_type = "UPDATE" if status else "CREATE"
    url = upload_template(args.bucket, args.stack, body, region)
    change_set = make_change_set(
        cfn, args.stack, change_set_type, url,
        [{"ParameterKey": k, "ParameterValue": v} for k, v in parameters.items()],
        tags,
    )
    if change_set["Status"] == "FAILED":
        reason = change_set.get("StatusReason", "")
        if any(text in reason for text in NO_CHANGE_REASONS):
            cfn.delete_change_set(StackName=args.stack, ChangeSetName=change_set["ChangeSetName"])
            print(f"{args.stack}: no changes")
            return
        raise SystemExit(f"{args.stack}: change set failed: {reason}")
    print_changes(change_set)
    execute(cfn, args.stack, change_set, change_set_type)
    outputs = cfn.describe_stacks(StackName=args.stack)["Stacks"][0].get("Outputs", [])
    for output in outputs:
        print(f"  output {output['OutputKey']} = {output['OutputValue']}")
    print(f"{args.stack}: {stack_status(cfn, args.stack)}")


if __name__ == "__main__":
    sys.exit(main())
