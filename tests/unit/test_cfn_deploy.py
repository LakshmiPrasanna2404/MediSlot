"""scripts/cfn_deploy.py: the paths that only run when a deployment is in an
unusual state (replacement of a protected resource, leftovers of a failed run,
importing hand-made resources)."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_script():
    spec = importlib.util.spec_from_file_location("cfn_deploy", ROOT / "scripts" / "cfn_deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeCfn:
    """Just enough of the CloudFormation client for cfn_deploy.main()."""

    def __init__(self, statuses, changes=None, existing_resources=()):
        self.statuses = list(statuses)
        self.changes = changes or []
        self.existing_resources = existing_resources
        self.calls = []
        self.change_set_bodies = []

    def _status(self):
        return self.statuses[0] if len(self.statuses) == 1 else self.statuses.pop(0)

    def describe_stacks(self, StackName):
        status = self._status()
        if status is None:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "ValidationError", "Message": "does not exist"}}, "DescribeStacks")
        return {"Stacks": [{"StackStatus": status, "Outputs": []}]}

    def describe_stack_events(self, StackName):
        return {"StackEvents": [{"LogicalResourceId": "Thing", "ResourceStatus": "CREATE_FAILED",
                                 "ResourceStatusReason": "boom"}]}

    def delete_stack(self, **kwargs):
        self.calls.append(("delete_stack", kwargs))

    def continue_update_rollback(self, **kwargs):
        self.calls.append(("continue_update_rollback", kwargs))

    def get_waiter(self, name):
        calls = self.calls

        class Waiter:
            def wait(self, **kwargs):
                calls.append(("wait", name))
        return Waiter()

    def create_change_set(self, **kwargs):
        self.calls.append(("create_change_set", kwargs["ChangeSetType"], kwargs.get("RoleARN"),
                           kwargs.get("ResourcesToImport")))

    def describe_change_set(self, StackName, ChangeSetName, NextToken=None):
        return {"Status": "CREATE_COMPLETE", "ChangeSetName": ChangeSetName, "Changes": self.changes}

    def delete_change_set(self, **kwargs):
        self.calls.append(("delete_change_set", kwargs["ChangeSetName"]))

    def execute_change_set(self, **kwargs):
        self.calls.append(("execute_change_set", kwargs["ChangeSetName"]))

    def get_paginator(self, name):
        resources = self.existing_resources

        class Paginator:
            def paginate(self, **kwargs):
                yield {"StackResourceSummaries": [{"LogicalResourceId": r} for r in resources]}
        return Paginator()

    def get_template(self, **kwargs):
        return {"TemplateBody": json.dumps({"Parameters": {"Environment": {"Type": "String"}},
                                            "Resources": {"Existing": {"Type": "AWS::SNS::Topic"}}})}


@pytest.fixture
def run(monkeypatch, tmp_path):
    def _run(fake, args, template=None, exists=lambda *a: True):
        module = load_script()
        uploaded = []
        monkeypatch.setattr(module.boto3, "client", lambda name, **kw: fake if name == "cloudformation" else _Sts())
        monkeypatch.setattr(module.boto3.session, "Session", lambda: type("S", (), {"region_name": "us-east-1"})())
        monkeypatch.setattr(module, "upload_template", lambda bucket, stack, body, region: uploaded.append(body) or "url")
        monkeypatch.setattr(module, "physical_exists", exists)
        monkeypatch.setattr(module.time, "sleep", lambda s: None)
        path = tmp_path / "t.yaml"
        path.write_text(json.dumps(template or {"Parameters": {"Environment": {"Type": "String"}},
                                                "Resources": {"Topic": {"Type": "AWS::SNS::Topic"}}}))
        monkeypatch.setattr(sys, "argv", ["cfn_deploy", "--stack", "medislot-dev-x", "--template", str(path),
                                          "--bucket", "b", "--param", "Environment=dev", *args])
        module.main()
        return uploaded
    return _run


class _Sts:
    def get_caller_identity(self):
        return {"Account": "111122223333"}


def test_update_without_service_role_omits_role_arn(run):
    fake = FakeCfn(["UPDATE_COMPLETE"], changes=[{"ResourceChange": {"Action": "Modify", "LogicalResourceId": "Topic",
                                                                      "ResourceType": "AWS::SNS::Topic", "Replacement": "False"}}])
    run(fake, [])
    created = [c for c in fake.calls if c[0] == "create_change_set"]
    assert created == [("create_change_set", "UPDATE", None, None)]
    assert any(c[0] == "execute_change_set" for c in fake.calls)


def test_protected_resource_replacement_is_refused_before_execution(run):
    change = {"ResourceChange": {"Action": "Modify", "LogicalResourceId": "MediSlotMySQL",
                                 "ResourceType": "AWS::RDS::DBInstance", "Replacement": "True",
                                 "Details": [{"Target": {"Name": "DBName", "RequiresRecreation": "Always"}}]}}
    fake = FakeCfn(["UPDATE_COMPLETE"], changes=[change])
    with pytest.raises(SystemExit) as error:
        run(fake, ["--protect", "MediSlotMySQL", "--role-arn", "arn:aws:iam::1:role/r"])
    assert "would REPLACE MediSlotMySQL (caused by: DBName)" in str(error.value)
    assert not any(c[0] == "execute_change_set" for c in fake.calls)
    assert any(c[0] == "delete_change_set" for c in fake.calls)


def test_unprotected_replacement_goes_ahead(run):
    change = {"ResourceChange": {"Action": "Modify", "LogicalResourceId": "WebInstance",
                                 "ResourceType": "AWS::EC2::Instance", "Replacement": "True", "Details": []}}
    fake = FakeCfn(["UPDATE_COMPLETE"], changes=[change])
    run(fake, ["--protect", "MediSlotMySQL"])
    assert any(c[0] == "execute_change_set" for c in fake.calls)


def test_rollback_complete_stack_is_deleted_and_created_again(run):
    fake = FakeCfn(["ROLLBACK_COMPLETE", "CREATE_COMPLETE"], changes=[])
    run(fake, ["--role-arn", "arn:aws:iam::1:role/r"])
    assert ("delete_stack", {"StackName": "medislot-dev-x", "RoleARN": "arn:aws:iam::1:role/r"}) in fake.calls
    assert ("create_change_set", "CREATE", "arn:aws:iam::1:role/r", None) in fake.calls


def test_update_rollback_failed_is_continued_before_the_update(run):
    fake = FakeCfn(["UPDATE_ROLLBACK_FAILED", "UPDATE_ROLLBACK_COMPLETE", "UPDATE_ROLLBACK_COMPLETE"], changes=[])
    run(fake, [])
    names = [c[0] for c in fake.calls]
    assert names.index("continue_update_rollback") < names.index("create_change_set")
    assert ("create_change_set", "UPDATE", None, None) in fake.calls


def test_hand_made_resource_is_imported_into_an_existing_stack(run):
    template = {"Parameters": {"Environment": {"Type": "String"}},
                "Resources": {"Existing": {"Type": "AWS::SNS::Topic"},
                              "RefreshTokensTable": {"Type": "AWS::DynamoDB::Table", "DeletionPolicy": "Retain",
                                                     "Properties": {"TableName": {"Fn::Sub": "medislot-${Environment}-refresh-tokens"}}}}}
    fake = FakeCfn(["UPDATE_COMPLETE"], changes=[], existing_resources=["Existing"])
    uploaded = run(fake, ["--import", "RefreshTokensTable"], template=template)
    imports = [c for c in fake.calls if c[0] == "create_change_set" and c[1] == "IMPORT"]
    assert imports[0][3] == [{"ResourceType": "AWS::DynamoDB::Table", "LogicalResourceId": "RefreshTokensTable",
                              "ResourceIdentifier": {"TableName": "medislot-dev-refresh-tokens"}}]
    import_template = json.loads(uploaded[0])
    assert set(import_template["Resources"]) == {"Existing", "RefreshTokensTable"}


def test_hand_made_resource_is_imported_when_the_stack_does_not_exist_yet(run):
    template = {"Parameters": {"Environment": {"Type": "String"}},
                "Resources": {"Other": {"Type": "AWS::SNS::Topic"},
                              "RefreshTokensTable": {"Type": "AWS::DynamoDB::Table", "DeletionPolicy": "Retain",
                                                     "Properties": {"TableName": {"Fn::Sub": "medislot-${Environment}-refresh-tokens"}}}},
                "Outputs": {"X": {"Value": "y"}}}
    fake = FakeCfn([None, "IMPORT_COMPLETE"], changes=[])
    uploaded = run(fake, ["--import", "RefreshTokensTable"], template=template)
    import_template = json.loads(uploaded[0])
    assert set(import_template["Resources"]) == {"RefreshTokensTable"}
    assert "Outputs" not in import_template
    types = [c[1] for c in fake.calls if c[0] == "create_change_set"]
    assert types == ["IMPORT", "UPDATE"]


def test_missing_hand_made_resource_is_simply_created(run):
    template = {"Parameters": {"Environment": {"Type": "String"}},
                "Resources": {"RefreshTokensTable": {"Type": "AWS::DynamoDB::Table", "DeletionPolicy": "Retain",
                                                     "Properties": {"TableName": "t"}}}}
    fake = FakeCfn([None, None, "CREATE_COMPLETE"], changes=[])
    run(fake, ["--import", "RefreshTokensTable"], template=template, exists=lambda *a: False)
    assert [c[1] for c in fake.calls if c[0] == "create_change_set"] == ["CREATE"]
