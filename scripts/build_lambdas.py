#!/usr/bin/env python3
"""Package every Lambda function and the web app into content-addressed zips.

    python scripts/build_lambdas.py --env dev [--ca-bundle build/rds-ca-bundle.pem] [--upload BUCKET]

Each zip holds the function's own .py files, the shared medislot_common
package and its pinned requirements (Linux x86_64 wheels for Python 3.12).
Zips are deterministic (sorted entries, fixed timestamps), so unchanged code
gives the same file name and CloudFormation sees no change for that function.

Writes build/manifest.json: {"functions": {name: s3_key}, "dashboard": s3_key}.
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build"

# function name (as in medislot-{env}-<name>) -> folder under lambda/
FUNCTIONS = {
    "auth": "auth",
    "authorizer": "authorizer",
    "doctor-profile": "doctor_profile",
    "photo-upload": "photo_upload",
    "consultation-types": "consultation_types",
    "availability": "availability",
    "schedule": "schedule",
    "patient-profile": "patient_profile",
    "doctor-search": "doctor_search",
    "slot-lookup": "slot_lookup",
    "slot-materialiser": "slot_materialiser",
    "db-migrate": "db_migrate",
}
NO_DATABASE = {"authorizer"}
PLATFORMS = ["manylinux2014_x86_64", "manylinux_2_28_x86_64", "manylinux_2_34_x86_64"]
FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def deterministic_zip(source_dir, destination, prefix=""):
    files = sorted(p for p in source_dir.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            info = zipfile.ZipInfo(prefix + path.relative_to(source_dir).as_posix(), date_time=FIXED_TIME)
            info.external_attr = (0o755 if path.stat().st_mode & 0o111 else 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def install_requirements(requirements, target):
    if not requirements.exists() or not requirements.read_text().strip():
        return
    command = [
        sys.executable, "-m", "pip", "install", "--quiet", "--no-compile", "--disable-pip-version-check",
        # Every dependency is pinned in requirements.txt. --no-deps keeps boto3 and
        # botocore out of the zip: the Lambda runtime already provides them.
        "--no-deps",
        "--target", str(target), "--implementation", "cp", "--python-version", "3.12", "--only-binary=:all:",
        "-r", str(requirements),
    ]
    for platform in PLATFORMS:
        command += ["--platform", platform]
    subprocess.run(command, check=True)  # noqa: S603 - fixed pip command, no user input


def build_function(name, folder, ca_bundle):
    stage = BUILD / "lambda" / name
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    source = ROOT / "lambda" / folder
    install_requirements(source / "requirements.txt", stage)
    for path in source.glob("*.py"):
        shutil.copy2(path, stage / path.name)
    shutil.copytree(ROOT / "lambda" / "shared" / "medislot_common", stage / "medislot_common",
                    ignore=shutil.ignore_patterns("__pycache__"))
    if folder == "db_migrate":
        shutil.copytree(ROOT / "database" / "migrations", stage / "migrations")
    if ca_bundle and name not in NO_DATABASE:
        shutil.copy2(ca_bundle, stage / "rds-ca-bundle.pem")
    temporary = BUILD / "dist" / f"{name}.zip"
    deterministic_zip(stage, temporary)
    final = BUILD / "dist" / f"{name}-{digest(temporary)}.zip"
    temporary.replace(final)
    return final


def build_dashboard():
    stage = BUILD / "web"
    shutil.rmtree(stage, ignore_errors=True)
    shutil.copytree(ROOT / "dashboard", stage / "dashboard",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"))
    temporary = BUILD / "dist" / "dashboard.zip"
    deterministic_zip(stage, temporary)
    final = BUILD / "dist" / f"dashboard-{digest(temporary)}.zip"
    temporary.replace(final)
    return final


def upload(bucket, key, path):
    import boto3
    from botocore.exceptions import ClientError

    s3 = boto3.client("s3")
    try:
        s3.head_object(Bucket=bucket, Key=key)
        print(f"  exists  s3://{bucket}/{key}")
        return
    except ClientError:
        pass
    s3.upload_file(str(path), bucket, key)
    print(f"  upload  s3://{bucket}/{key}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", required=True)
    parser.add_argument("--ca-bundle", type=Path, help="RDS CA bundle to pack next to the code")
    parser.add_argument("--upload", metavar="BUCKET", help="upload the zips to this bucket")
    args = parser.parse_args()

    (BUILD / "dist").mkdir(parents=True, exist_ok=True)
    manifest = {"functions": {}, "dashboard": None}
    for name, folder in FUNCTIONS.items():
        path = build_function(name, folder, args.ca_bundle)
        manifest["functions"][name] = f"medislot/{args.env}/lambda/{path.name}"
        print(f"built {path.name} ({path.stat().st_size // 1024} KiB)")
    web = build_dashboard()
    manifest["dashboard"] = f"medislot/{args.env}/dashboard/{web.name}"
    print(f"built {web.name}")

    if args.upload:
        for key in manifest["functions"].values():
            upload(args.upload, key, BUILD / "dist" / Path(key).name)
        upload(args.upload, manifest["dashboard"], web)
    (BUILD / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
