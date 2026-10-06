#!/usr/bin/env bash
# Build the GameLift-ready game server image and push it to ECR.
#
# The Go binaries (GameLift SDK wrapper + echo game server) are cross-compiled
# on the host for linux/amd64, then copied into a thin Amazon Linux 2023 image.
# Compiling on the host avoids depending on DNS/network inside the Docker build
# VM, which is unreliable on some corporate networks.
#
# Usage: ./build.sh <ecr-repository-uri> [tag]
set -euo pipefail

URI="${1:?usage: ./build.sh <ecr-repository-uri> [tag]}"
TAG="${2:-v1-$(date +%Y%m%d%H%M%S)}"
SDK_VERSION="${GAMELIFT_SDK_VERSION:-5.2.0}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "==> Building GameLift SDK wrapper (Go Server SDK ${SDK_VERSION})"
cp -R "$HERE/wrapper-src" "$WORK/wrapper"
mkdir -p "$WORK/wrapper/gamelift-server-sdk"
curl -sSfL "https://gamelift-server-sdk-release.s3.us-west-2.amazonaws.com/go/GameLift-Go-ServerSDK-${SDK_VERSION}.zip" -o "$WORK/sdk.zip"
unzip -q "$WORK/sdk.zip" -d "$WORK/wrapper/gamelift-server-sdk"
mkdir -p "$HERE/bin"
(cd "$WORK/wrapper" && go mod tidy && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o "$HERE/bin/gameliftwrapper" .)

echo "==> Building echo game server"
(cd "$HERE/echo" && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o "$HERE/bin/echoserver" .)

echo "==> Building and pushing ${URI}:${TAG}"
docker build --platform linux/amd64 -t "${URI}:${TAG}" -t "${URI}:latest" "$HERE"
docker push "${URI}:${TAG}"
docker push "${URI}:latest"
echo "IMAGE=${URI}:${TAG}"
