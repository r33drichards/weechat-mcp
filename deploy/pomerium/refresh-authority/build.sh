#!/usr/bin/env bash
# Builds a local image only. Does not push, deploy, log in, or touch live volumes.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
root=$(cd -- "$here/../../.." && pwd)
output_image=${1:-weechat-gateway-refresh:76042ed-v1}
# The Dockerfile fetches and verifies the exact source commit, checks/applies the
# patch, runs focused race tests, and builds Pomerium including UI/Envoy assets.
docker build --file "$root/deploy/pomerium/Dockerfile" --tag "$output_image" "$root"
printf 'Built local candidate: %s\nNo deployment performed.\n' "$output_image"
