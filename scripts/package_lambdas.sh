#!/usr/bin/env bash
# Packages each Lambda into a zip and uploads to S3.
# Usage: ./scripts/package_lambdas.sh <s3-bucket-name>
set -euo pipefail

BUCKET="${1:?Usage: $0 <s3-bucket-name>}"
LAMBDAS_DIR="$(dirname "$0")/../lambdas"

for fn in processor validator pr_creator; do
    echo "==> Packaging $fn"
    dir="$LAMBDAS_DIR/$fn"
    tmp=$(mktemp -d)

    pip3 install -r "$dir/requirements.txt" -t "$tmp" -q
    cp "$dir/handler.py" "$tmp/"

    zip_path="/tmp/${fn}.zip"
    (cd "$tmp" && zip -r "$zip_path" . -q)
    rm -rf "$tmp"

    echo "    Uploading ${fn}.zip to s3://${BUCKET}/"
    aws s3 cp "$zip_path" "s3://${BUCKET}/${fn}.zip"
    echo "    Done"
done

echo "All Lambdas packaged and uploaded."
