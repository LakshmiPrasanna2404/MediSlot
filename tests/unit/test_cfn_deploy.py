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
        self.fail_reason = None

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
        if self.fail_reason and not ChangeSetName.startswith("import"):
            return {"Status": "FAILED", "ChangeSetName": ChangeSetName, "ChangeSetId": "arn:cs/" + ChangeSetName,
                    "StatusReason": self.fail_reason}
        return {"Status": "CREATE_COMPLETE", "ChangeSetName": ChangeSetName, "Changes": self.changes}

    def describe_events(self, **kwargs):
        self.calls.append(("describe_events", kwargs.get("Filters")))
        return {"OperationEvents": [{"LogicalResourceId": "SlotsTable", "ResourceType": "AWS::DynamoDB::Table",
                                     "ValidationName": "ResourceExistenceCheck",
                                     "ValidationStatusReason": "Resource medislot-dev-slots already exists"}]}

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
    fake = FakeCfn([None, "CREATE_COMPLETE"], changes=[])
    run(fake, ["--import", "RefreshTokensTable"], template=template, exists=lambda *a: False)
    assert [c[1] for c in fake.calls if c[0] == "create_change_set"] == ["CREATE"]


def test_existing_named_resource_is_adopted_without_being_listed(run):
    """The template would create SlotsTable, but a table with that name already exists."""
    template = {"Parameters": {"Environment": {"Type": "String"}},
                "Resources": {"Existing": {"Type": "AWS::SNS::Topic"},
                              "SlotsTable": {"Type": "AWS::DynamoDB::Table",
                                             "Properties": {"TableName": {"Fn::Sub": "medislot-${Environment}-slots"}}}}}
    fake = FakeCfn(["UPDATE_COMPLETE"], changes=[], existing_resources=["Existing"])
    uploaded = run(fake, [], template=template)
    imports = [c for c in fake.calls if c[0] == "create_change_set" and c[1] == "IMPORT"]
    assert imports[0][3][0]["ResourceIdentifier"] == {"TableName": "medislot-dev-slots"}
    adopted = json.loads(uploaded[0])["Resources"]["SlotsTable"]
    assert adopted["DeletionPolicy"] == "Retain"   # added: an import requires one


def test_resource_depending_on_a_missing_resource_is_not_imported(run):
    template = {"Parameters": {"Environment": {"Type": "String"}},
                "Resources": {"Api": {"Type": "AWS::ApiGateway::RestApi"},
                              "BaseUrl": {"Type": "AWS::SSM::Parameter",
                                          "Properties": {"Name": "/medislot/dev/api/base-url",
                                                         "Value": {"Fn::Sub": "https://${Api}.example"}}}}}
    fake = FakeCfn(["UPDATE_COMPLETE"], changes=[], existing_resources=[])
    run(fake, [], template=template)
    assert [c[1] for c in fake.calls if c[0] == "create_change_set"] == ["UPDATE"]


def test_failed_validation_prints_the_resource_and_reason(run, capsys):
    fake = FakeCfn(["UPDATE_COMPLETE"], changes=[])
    fake.fail_reason = "The following hook(s)/validation failed: [AWS::EarlyValidation::ResourceExistenceCheck]."
    with pytest.raises(SystemExit):
        run(fake, [], exists=lambda *a: False)
    out = capsys.readouterr().out
    assert ("describe_events", {"FailedEvents": True}) in fake.calls
    assert "SlotsTable (AWS::DynamoDB::Table) ResourceExistenceCheck: Resource medislot-dev-slots already exists" in out


def test_references_follow_ref_getatt_and_sub():
    module = load_script()
    fragment = {"A": {"Ref": "Table"}, "B": {"Fn::GetAtt": ["Role", "Arn"]},
                "C": {"Fn::Sub": "${Bucket.Arn}/x/${AWS::Region}/${Environment}"},
                "D": {"Fn::Sub": ["${Local}-${Topic}", {"Local": {"Ref": "Queue"}}]}}
    assert module.references(fragment) == {"Table", "Role", "Bucket", "Environment", "Topic", "Queue"}


def test_parameters_not_passed_keep_their_deployed_values():
    module = load_script()
    template = {"Parameters": {"Environment": {}, "DBName": {"Default": "medislot"}, "NewSetting": {"Default": "x"}}}
    params = module.change_set_parameters(template, {"Environment": "dev"}, {"Environment": "dev", "DBName": "medislotdb"})
    assert params == [{"ParameterKey": "Environment", "ParameterValue": "dev"},
                      {"ParameterKey": "DBName", "UsePreviousValue": True}]   # NewSetting: template default


def test_unknown_parameter_is_rejected():
    module = load_script()
    with pytest.raises(SystemExit, match="Typo"):
        module.change_set_parameters({"Parameters": {"Environment": {}}}, {"Typo": "1"}, {})


def test_protected_differences_are_reported_with_parameters_resolved(capsys):
    module = load_script()
    deployed = {"Resources": {"MediSlotMySQL": {"Type": "AWS::RDS::DBInstance",
                                                "Properties": {"DBName": {"Ref": "DBName"}, "StorageEncrypted": False}}}}

    class Cfn:
        def get_template(self, **kwargs):
            return {"TemplateBody": json.dumps(deployed)}

    new = {"Parameters": {"DBName": {"Default": "medislot"}},
           "Resources": {"MediSlotMySQL": {"Type": "AWS::RDS::DBInstance",
                                           "Properties": {"DBName": {"Ref": "DBName"}, "StorageEncrypted": True}}}}
    module.report_protected_differences(Cfn(), "s", new, {"MediSlotMySQL"}, {}, {"DBName": "medislot"})
    out = capsys.readouterr().out
    assert "MediSlotMySQL.StorageEncrypted deployed=false new=true" in out
    assert "DBName" not in out    # same value once the parameter is resolved
