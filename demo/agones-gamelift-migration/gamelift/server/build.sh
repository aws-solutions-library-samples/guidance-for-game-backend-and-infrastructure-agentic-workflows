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
GAME_PORT="${GAME_PORT:-7654}"
SDK_VERSION="5.2.0"
# SHA-256 of GameLift-Go-ServerSDK-5.2.0.zip; the build stops if the download differs.
SDK_SHA256="ec1813c1b0f02423cbe669413bc03e9c26495098bbef8119fc0408b95099e3be"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "==> Building GameLift SDK wrapper (Go Server SDK ${SDK_VERSION})"
cp -R "$HERE/wrapper-src" "$WORK/wrapper"
mkdir -p "$WORK/wrapper/gamelift-server-sdk"
curl -sSfL "https://gamelift-server-sdk-release.s3.us-west-2.amazonaws.com/go/GameLift-Go-ServerSDK-${SDK_VERSION}.zip" -o "$WORK/sdk.zip"
echo "${SDK_SHA256}  $WORK/sdk.zip" | shasum -a 256 -c - >/dev/null || { echo "Server SDK checksum mismatch" >&2; exit 1; }
unzip -q "$WORK/sdk.zip" -d "$WORK/wrapper/gamelift-server-sdk"
mkdir -p "$HERE/bin"
(cd "$WORK/wrapper" && go mod tidy && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o "$HERE/bin/gameliftwrapper" .)

echo "==> Building echo game server"
(cd "$HERE/echo" && CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -o "$HERE/bin/echoserver" .)

echo "==> Building and pushing ${URI}:${TAG}"
docker build --platform linux/amd64 --build-arg GAME_PORT="${GAME_PORT}" -t "${URI}:${TAG}" "$HERE"
docker push "${URI}:${TAG}"
echo "IMAGE=${URI}:${TAG}"
