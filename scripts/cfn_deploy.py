#!/usr/bin/env python3
"""Deploy one CloudFormation stack through a change set.

    python scripts/cfn_deploy.py --stack medislot-dev-network \
        --template infrastructure/network-stack.yaml \
        --param Environment=dev --param S3PrefixListId=pl-123 \
        --bucket <artifacts bucket> [--role-arn <CloudFormation execution role>] \
        [--import RefreshTokensTable --import DoctorProfileFilesBucket]

Without --role-arn, CloudFormation acts with the caller's credentials (network,
data and iam stacks); with it, CloudFormation assumes that service role.

1. Uploads the template to S3 (templates over 51,200 bytes must come from S3).
2. Resources that already exist in the account but not in the stack (created
   by hand, or left behind by an earlier stack) are first brought under
   CloudFormation with an IMPORT change set: every table, bucket, database,
   subnet group, log group, SSM parameter, Lambda function, IAM role and alarm
   the template would otherwise create under a name that is already taken
   (--import names extra ones explicitly). Afterwards it is a no-op.
3. Creates a change set, prints every change (action, resource, replacement)
   to the run log, executes it and waits. A change set with no changes is
   deleted and the step passes.
4. --protect: if the change set would REPLACE one of these resources (a
   database or a table), it is deleted unexecuted and the step fails with the
   property that forces the replacement. Nothing in the stack changes.
5. Recovers stacks left by earlier failed runs: ROLLBACK_COMPLETE (empty, is
   deleted and created again), REVIEW_IN_PROGRESS (created again) and
   UPDATE_ROLLBACK_FAILED (the rollback is continued first).

Every stack update goes through a change set; nothing is changed directly.
"""

import argparse
import hashlib
import json
import re
import sys
import time

import boto3
from botocore.exceptions import ClientError, WaiterError

CAPABILITIES = ["CAPABILITY_NAMED_IAM", "CAPABILITY_AUTO_EXPAND"]
NO_CHANGE_REASONS = ("didn't contain changes", "No updates are to be performed")
# Resource types that can be adopted into a stack when they already exist in
# the account under the name the template gives them (import identifier).
IMPORTABLE = {
    "AWS::DynamoDB::Table": "TableName",
    "AWS::S3::Bucket": "BucketName",
    "AWS::RDS::DBInstance": "DBInstanceIdentifier",
    "AWS::RDS::DBSubnetGroup": "DBSubnetGroupName",
    "AWS::Logs::LogGroup": "LogGroupName",
    "AWS::SSM::Parameter": "Name",
    "AWS::Lambda::Function": "FunctionName",
    "AWS::IAM::Role": "RoleName",
    "AWS::CloudWatch::Alarm": "AlarmName",
}
PSEUDO = {"AWS::AccountId", "AWS::Region", "AWS::Partition", "AWS::StackName", "AWS::StackId",
          "AWS::URLSuffix", "AWS::NoValue", "AWS::NotificationARNs"}


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
            break
        time.sleep(3)
    changes, token = list(result.get("Changes", [])), result.get("NextToken")
    while token:
        page = cfn.describe_change_set(StackName=stack, ChangeSetName=name, NextToken=token)
        changes.extend(page.get("Changes", []))
        token = page.get("NextToken")
    result["Changes"] = changes
    return result


def refuse_replacements(cfn, stack, change_set, protected):
    """Stop before a protected resource (database, table) would be replaced."""
    blocked = []
    for change in change_set.get("Changes", []):
        rc = change["ResourceChange"]
        if rc["LogicalResourceId"] in protected and rc.get("Replacement") == "True":
            causes = sorted({
                d["Target"].get("Name") or d["Target"].get("Attribute", "")
                for d in rc.get("Details", [])
                if d.get("Target", {}).get("RequiresRecreation") in ("Always", "Conditionally")
            })
            blocked.append(f"{rc['LogicalResourceId']} (caused by: {', '.join(c for c in causes if c) or 'unknown'})")
    if blocked:
        cfn.delete_change_set(StackName=stack, ChangeSetName=change_set["ChangeSetName"])
        raise SystemExit(
            f"::error::{stack}: this update would REPLACE {'; '.join(blocked)}. Nothing was changed. "
            "The deployed stack differs from the template in a property that cannot be changed in place; "
            "make the template match the deployed value."
        )


def continue_rollback(cfn, stack, role_arn):
    print(f"{stack} is UPDATE_ROLLBACK_FAILED; continuing the rollback first")
    kwargs = {"StackName": stack}
    if role_arn:
        kwargs["RoleARN"] = role_arn
    cfn.continue_update_rollback(**kwargs)
    for _ in range(360):
        status = stack_status(cfn, stack)
        if status == "UPDATE_ROLLBACK_COMPLETE":
            return status
        if status == "UPDATE_ROLLBACK_FAILED":
            print_failures(cfn, stack)
            raise SystemExit(
                f"::error::{stack}: the rollback cannot finish on its own. In the CloudFormation console choose "
                "Stack actions > Continue update rollback and skip the resource named above, then re-run."
            )
        time.sleep(10)
    raise SystemExit(f"{stack}: rollback did not finish in time")


def print_changes(change_set):
    changes = change_set.get("Changes", [])
    print(f"Change set {change_set['ChangeSetName']}: {len(changes)} change(s)")
    print(f"  {'Action':<8} {'Logical ID':<45} {'Type':<38} Replacement")
    for change in changes:
        rc = change["ResourceChange"]
        print(f"  {rc['Action']:<8} {rc['LogicalResourceId']:<45} {rc['ResourceType']:<38} {rc.get('Replacement', '-')}")


STARTED = ("CREATE_IN_PROGRESS", "UPDATE_IN_PROGRESS", "IMPORT_IN_PROGRESS")


def print_failures(cfn, stack):
    """Every failed resource of the latest operation, the first one (the cause) first.

    A large update that fails rolls back many resources, and those rollback
    events push the cause far down the list, so this reads back to the event
    that started the operation instead of a fixed number of events."""
    failures = []
    request = {"StackName": stack}
    for _ in range(20):  # at most 20 pages of events
        page = cfn.describe_stack_events(**request)
        started = False
        for event in page["StackEvents"]:
            if (event.get("ResourceType") == "AWS::CloudFormation::Stack" and event["ResourceStatus"] in STARTED
                    and event.get("ResourceStatusReason") == "User Initiated"):
                started = True
                break
            if "FAILED" in event["ResourceStatus"]:
                failures.append(event)
        if started or "NextToken" not in page:
            break
        request["NextToken"] = page["NextToken"]
    for number, event in enumerate(reversed(failures)):
        label = "cause" if number == 0 else "then"
        print(f"::error::{label}: {event['LogicalResourceId']} {event['ResourceStatus']}: "
              f"{event.get('ResourceStatusReason', '')}")


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


def make_change_set(cfn, stack, change_set_type, url, parameters, role_arn, tags, resources_to_import=None):
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
    if role_arn:
        kwargs["RoleARN"] = role_arn
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
    """The physical name a template gives a resource, or None if it is not a plain value."""
    names = {**parameters, "AWS::AccountId": account, "AWS::Region": region, "AWS::Partition": "aws"}
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and set(value) == {"Ref"} and value["Ref"] in names:
        return names[value["Ref"]]
    if isinstance(value, dict) and "Fn::Sub" in value and isinstance(value["Fn::Sub"], str):
        text = value["Fn::Sub"]
        for key, val in names.items():
            text = text.replace("${" + key + "}", val)
        return None if "${" in text else text
    return None


def physical_exists(resource_type, identifier):
    try:
        if resource_type == "AWS::DynamoDB::Table":
            boto3.client("dynamodb").describe_table(TableName=identifier)
        elif resource_type == "AWS::S3::Bucket":
            boto3.client("s3").head_bucket(Bucket=identifier)
        elif resource_type == "AWS::RDS::DBInstance":
            boto3.client("rds").describe_db_instances(DBInstanceIdentifier=identifier)
        elif resource_type == "AWS::RDS::DBSubnetGroup":
            boto3.client("rds").describe_db_subnet_groups(DBSubnetGroupName=identifier)
        elif resource_type == "AWS::Logs::LogGroup":
            groups = boto3.client("logs").describe_log_groups(logGroupNamePrefix=identifier)["logGroups"]
            return any(g["logGroupName"] == identifier for g in groups)
        elif resource_type == "AWS::SSM::Parameter":
            boto3.client("ssm").get_parameter(Name=identifier)
        elif resource_type == "AWS::Lambda::Function":
            boto3.client("lambda").get_function(FunctionName=identifier)
        elif resource_type == "AWS::IAM::Role":
            boto3.client("iam").get_role(RoleName=identifier)
        elif resource_type == "AWS::CloudWatch::Alarm":
            return bool(boto3.client("cloudwatch").describe_alarms(AlarmNames=[identifier])["MetricAlarms"])
        else:
            return False
        return True
    except ClientError:
        return False


def references(node):
    """Logical IDs and parameter names a template fragment refers to."""
    found = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "Ref" and isinstance(value, str):
                found.add(value)
            elif key == "Fn::GetAtt":
                target = value[0] if isinstance(value, list) else str(value).split(".")[0]
                found.add(target)
            elif key == "Fn::Sub":
                text = value[0] if isinstance(value, list) else value
                local = set(value[1]) if isinstance(value, list) and len(value) > 1 else set()
                for name in re.findall(r"\$\{([^}!]+)\}", text):
                    name = name.split(".")[0]
                    if name not in local:
                        found.add(name)
                if isinstance(value, list) and len(value) > 1:
                    found |= references(value[1])
                continue
            found |= references(value)
    elif isinstance(node, list):
        for item in node:
            found |= references(item)
    return found - PSEUDO


def stack_parameters(cfn, stack):
    stacks = cfn.describe_stacks(StackName=stack)["Stacks"]
    return {p["ParameterKey"]: p.get("ParameterValue") for p in stacks[0].get("Parameters", [])}


def change_set_parameters(new_template, given, previous):
    """Like `aws cloudformation deploy`: a parameter that is not passed keeps the
    value the stack already has, so a deploy never quietly resets a setting
    (an instance class, a database name) to the template default."""
    result = []
    for key in new_template.get("Parameters", {}):
        if key in given:
            result.append({"ParameterKey": key, "ParameterValue": given[key]})
        elif key in previous:
            result.append({"ParameterKey": key, "UsePreviousValue": True})
    unknown = sorted(set(given) - set(new_template.get("Parameters", {})))
    if unknown:
        raise SystemExit(f"--param given for parameters the template does not have: {', '.join(unknown)}")
    return result


def report_protected_differences(cfn, stack, new_template, protected, given, previous):
    """Before the change set: show how each protected resource in the deployed
    template differs from the new one, with parameters resolved on both sides."""
    if not protected:
        return
    deployed = cfn_json(cfn.get_template(StackName=stack, TemplateStage="Original")["TemplateBody"])
    old_values = {k: v for k, v in previous.items()}
    new_values = {k: given.get(k, previous.get(k, (p or {}).get("Default")))
                  for k, p in new_template.get("Parameters", {}).items()}

    def resolve(node, values):
        if isinstance(node, dict):
            if set(node) == {"Ref"} and node["Ref"] in values:
                return values[node["Ref"]]
            return {k: resolve(v, values) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v, values) for v in node]
        return node

    for logical_id in sorted(protected):
        old = deployed.get("Resources", {}).get(logical_id)
        new = new_template.get("Resources", {}).get(logical_id)
        if not old or not new:
            continue
        old_props = resolve(old.get("Properties", {}), old_values)
        new_props = resolve(new.get("Properties", {}), new_values)
        for key in sorted(set(old_props) | set(new_props)):
            if old_props.get(key) != new_props.get(key):
                print(f"  note: {logical_id}.{key} deployed={json.dumps(old_props.get(key))[:120]} "
                      f"new={json.dumps(new_props.get(key))[:120]}")


def print_validation_failures(cfn, stack, change_set):
    """Early validation (e.g. ResourceExistenceCheck) puts its details in DescribeEvents."""
    try:
        events = cfn.describe_events(StackName=stack, ChangeSetName=change_set.get("ChangeSetId") or
                                     change_set["ChangeSetName"], Filters={"FailedEvents": True})
    except Exception as error:  # noqa: BLE001 - details are a bonus; never hide the real failure
        print(f"(could not read validation details: {error})")
        return
    for event in events.get("OperationEvents", []):
        what = event.get("LogicalResourceId") or event.get("ValidationPath") or ""
        reason = event.get("ValidationStatusReason") or event.get("ResourceStatusReason") or event.get("HookStatusReason") or ""
        print(f"::error::{what} ({event.get('ResourceType', '')}) {event.get('ValidationName', '')}: {reason}")


def import_existing(cfn, stack, new_template, logical_ids, parameters, role_arn, bucket, region, tags,
                    stack_exists=True):
    """Adopt resources that already exist in the account but not in the stack.

    Covers the resources named in --import and every resource of an importable
    type that the template would otherwise create under a name that is already
    taken (CloudFormation would refuse the change set). Imported once; on
    later runs they are already in the stack and nothing happens.
    """
    account = boto3.client("sts").get_caller_identity()["Account"]
    in_stack = set()
    if stack_exists:
        for page in cfn.get_paginator("list_stack_resources").paginate(StackName=stack):
            in_stack.update(r["LogicalResourceId"] for r in page["StackResourceSummaries"])
        current = cfn_json(cfn.get_template(StackName=stack, TemplateStage="Original")["TemplateBody"])
    else:
        # A new stack can be created by importing: the template then holds only
        # the resources being imported; the full template follows as an update.
        current = {k: v for k, v in new_template.items() if k not in ("Resources", "Outputs")}
        current["Resources"] = {}
    current.setdefault("Resources", {})
    current_params = current.setdefault("Parameters", {})
    new_resources = new_template.get("Resources", {})

    for logical_id in logical_ids:
        if logical_id not in new_resources or new_resources[logical_id]["Type"] not in IMPORTABLE:
            raise SystemExit(f"--import {logical_id}: not an importable resource in the template")
    candidates = []
    for logical_id, resource in new_resources.items():
        if logical_id in in_stack or resource["Type"] not in IMPORTABLE:
            continue
        identifier_key = IMPORTABLE[resource["Type"]]
        identifier = resolve_identifier(resource.get("Properties", {}).get(identifier_key), parameters, account, region)
        if identifier and physical_exists(resource["Type"], identifier):
            candidates.append((logical_id, identifier_key, identifier))

    # A resource can only be imported if everything it refers to is already in
    # the stack, is a parameter, or is imported with it.
    chosen = {c[0]: c for c in candidates}
    changed = True
    while changed:
        changed = False
        for logical_id in list(chosen):
            resource = new_resources[logical_id]
            refs = references(resource.get("Properties", {})) | references(resource.get("Condition", ""))
            missing = [r for r in refs if r in new_resources and r not in in_stack and r not in chosen]
            condition = resource.get("Condition")
            if missing or (condition and condition not in current.get("Conditions", {})):
                print(f"  {logical_id} exists but cannot be imported yet (depends on {missing or condition})")
                del chosen[logical_id]
                changed = True

    to_import = []
    added_params = {}
    for logical_id, identifier_key, identifier in chosen.values():
        resource = dict(new_resources[logical_id])
        resource.pop("DependsOn", None)
        resource.setdefault("DeletionPolicy", "Retain")  # required for an import
        for name in references(resource.get("Properties", {})):
            if name in new_template.get("Parameters", {}) and name not in current_params:
                current_params[name] = new_template["Parameters"][name]
                added_params[name] = parameters.get(name)
        current["Resources"][logical_id] = resource
        to_import.append({"ResourceType": resource["Type"], "LogicalResourceId": logical_id,
                          "ResourceIdentifier": {identifier_key: identifier}})
    if not to_import:
        return False
    print(f"Importing {len(to_import)} existing resource(s) into {stack}:")
    for item in to_import:
        print(f"  {item['LogicalResourceId']} <- {item['ResourceIdentifier']}")
    body = json.dumps(current)
    url = upload_template(bucket, stack + "-import", body, region)
    previous = []
    for key in current_params:
        if stack_exists and key not in added_params:
            previous.append({"ParameterKey": key, "UsePreviousValue": True})
        elif parameters.get(key) is not None:
            previous.append({"ParameterKey": key, "ParameterValue": parameters[key]})
    change_set = make_change_set(cfn, stack, "IMPORT", url, previous, role_arn, tags, to_import)
    if change_set["Status"] == "FAILED":
        print_validation_failures(cfn, stack, change_set)
        raise SystemExit(f"Import change set failed: {change_set.get('StatusReason')}")
    print_changes(change_set)
    execute(cfn, stack, change_set, "IMPORT")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stack", required=True)
    parser.add_argument("--template", required=True)
    parser.add_argument("--param", action="append", default=[])
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--role-arn", default=None, help="CloudFormation service role (optional)")
    parser.add_argument("--bucket", required=True, help="bucket for the uploaded template")
    parser.add_argument("--import", dest="imports", action="append", default=[],
                        help="logical ID of a resource that may already exist outside the stack")
    parser.add_argument("--protect", action="append", default=[],
                        help="logical ID that must never be replaced (the step fails instead)")
    args = parser.parse_args()

    session = boto3.session.Session()
    region = session.region_name
    cfn = boto3.client("cloudformation")
    body = open(args.template, encoding="utf-8").read()
    parameters = key_values(args.param)
    # No stack-level tags by default: they would be pushed to every resource
    # in the stack. The templates tag their own resources.
    tags = key_values(args.tag)

    status = stack_status(cfn, args.stack)
    if status == "ROLLBACK_COMPLETE":
        # A failed first create leaves an empty stack that can only be deleted.
        # Print why it failed, delete it, and create it again.
        print(f"{args.stack} is ROLLBACK_COMPLETE after a failed create. Earlier failure:")
        for event in cfn.describe_stack_events(StackName=args.stack)["StackEvents"]:
            if event["ResourceStatus"].endswith("FAILED"):
                print(f"  {event['LogicalResourceId']}: {event.get('ResourceStatusReason', '')}")
        print("Deleting the empty stack and creating it again.")
        delete_args = {"StackName": args.stack}
        if args.role_arn:
            delete_args["RoleARN"] = args.role_arn
        cfn.delete_stack(**delete_args)
        cfn.get_waiter("stack_delete_complete").wait(StackName=args.stack, WaiterConfig={"Delay": 10, "MaxAttempts": 90})
        status = None
    if status == "REVIEW_IN_PROGRESS":
        # A CREATE change set was made but never executed (it failed); the stack
        # has no resources yet, so start again with a new CREATE change set.
        status = None
    if status == "UPDATE_ROLLBACK_FAILED":
        status = continue_rollback(cfn, args.stack, args.role_arn)
    if status and status.endswith("_IN_PROGRESS"):
        raise SystemExit(f"{args.stack} is busy ({status}); try again when it finishes")

    # Adopt resources the template would create but that already exist.
    if import_existing(cfn, args.stack, cfn_json(body), args.imports, parameters, args.role_arn,
                       args.bucket, region, tags, stack_exists=bool(status)):
        status = stack_status(cfn, args.stack)

    change_set_type = "UPDATE" if status else "CREATE"
    new_template = cfn_json(body)
    previous = stack_parameters(cfn, args.stack) if status else {}
    change_set_params = change_set_parameters(new_template, parameters, previous)
    kept = [p["ParameterKey"] for p in change_set_params if p.get("UsePreviousValue")]
    if kept:
        print(f"Parameters kept at their deployed values: {', '.join(kept)}")
    if status:
        report_protected_differences(cfn, args.stack, new_template, set(args.protect), parameters, previous)
    url = upload_template(args.bucket, args.stack, body, region)
    change_set = make_change_set(cfn, args.stack, change_set_type, url, change_set_params, args.role_arn, tags)
    if change_set["Status"] == "FAILED":
        reason = change_set.get("StatusReason", "")
        if any(text in reason for text in NO_CHANGE_REASONS):
            cfn.delete_change_set(StackName=args.stack, ChangeSetName=change_set["ChangeSetName"])
            print(f"{args.stack}: no changes")
            return
        print_validation_failures(cfn, args.stack, change_set)
        raise SystemExit(f"{args.stack}: change set failed: {reason}")
    print_changes(change_set)
    refuse_replacements(cfn, args.stack, change_set, set(args.protect))
    execute(cfn, args.stack, change_set, change_set_type)
    outputs = cfn.describe_stacks(StackName=args.stack)["Stacks"][0].get("Outputs", [])
    for output in outputs:
        print(f"  output {output['OutputKey']} = {output['OutputValue']}")
    print(f"{args.stack}: {stack_status(cfn, args.stack)}")


if __name__ == "__main__":
    sys.exit(main())
