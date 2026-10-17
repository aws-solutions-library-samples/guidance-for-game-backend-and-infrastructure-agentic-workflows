#!/bin/bash
# Build and push the frontend image in CodeBuild instead of local Docker.
#
# Used by scripts/deploy.sh when Docker is unavailable on the deploy host. The
# base infrastructure stack provides the amd64 CodeBuild project, its scoped
# role, and the source bucket.
#
# Usage:
#   build-frontend-codebuild.sh build <image-tag>
#   build-frontend-codebuild.sh package <ui-dir> <output-zip>
#
# `package` archives only files git would track (tracked plus untracked files
# that .gitignore does not exclude), so ui/.env.local, node_modules, and .next
# never leave the host. Outside a git checkout it falls back to explicit
# excludes. The CodeBuild Docker build also applies ui/.dockerignore.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
AWS_REGION="${AWS_REGION:-us-west-2}"
PROJECT_NAME="${PROJECT_NAME:-game-agent}"
BUILD_TIMEOUT_SECONDS="${GBAW_FRONTEND_BUILD_TIMEOUT_SECONDS:-2400}"
POLL_SECONDS="${GBAW_FRONTEND_BUILD_POLL_SECONDS:-15}"

fail() {
    echo "❌ $*" >&2
    exit 1
}

package_ui() {
    local ui_dir=$1
    local output=$2

    command -v zip > /dev/null 2>&1 || fail "zip is required to package the frontend source."
    [ -f "$ui_dir/Dockerfile" ] || fail "No Dockerfile in $ui_dir."
    rm -f "$output"

    if git -C "$ui_dir" rev-parse --is-inside-work-tree > /dev/null 2>&1; then
        (
            cd "$ui_dir"
            git ls-files --cached --others --exclude-standard -z . |
                while IFS= read -r -d '' path; do
                    # Skip tracked files deleted in the working tree. An if keeps the
                    # loop's status zero under pipefail when the last path is skipped.
                    if [ -f "$path" ]; then printf '%s\n' "$path"; fi
                done |
                zip -q -X "$output" -@
        )
    else
        (
            cd "$ui_dir"
            zip -q -X -r "$output" . \
                -x 'node_modules/*' '.next/*' '.env.local' '.env.*.local' 'coverage/*' \
                   'test-results/*' 'playwright-report*' '.git/*'
        )
    fi

    if unzip -Z1 "$output" | grep -Eq '(^|/)\.env(\.[^/]*)?\.local$|^node_modules/'; then
        rm -f "$output"
        fail "Refusing to upload: the source archive contains local environment files or node_modules."
    fi
}

stack_output() {
    aws cloudformation describe-stacks \
        --stack-name "${PROJECT_NAME}-infrastructure" \
        --region "$AWS_REGION" \
        --query "Stacks[0].Outputs[?OutputKey==\`$1\`].OutputValue" \
        --output text
}

build() {
    local image_tag=$1
    [[ "$image_tag" =~ ^[A-Za-z0-9_.-]{1,128}$ ]] || fail "Invalid image tag: $image_tag"

    local project bucket
    project=$(stack_output FrontendBuildProjectName)
    bucket=$(stack_output FrontendBuildSourceBucketName)
    if [ -z "$project" ] || [ "$project" = "None" ] || [ -z "$bucket" ] || [ "$bucket" = "None" ]; then
        fail "Base stack does not export the frontend CodeBuild project. Redeploy the base infrastructure stack."
    fi

    local archive key
    archive="$(mktemp -d)/frontend-source.zip"
    key="frontend/${image_tag}.zip"
    echo "   Packaging ui/ source..."
    package_ui "$PROJECT_ROOT/ui" "$archive"
    echo "   Uploading source to s3://${bucket}/${key}"
    aws s3 cp "$archive" "s3://${bucket}/${key}" --region "$AWS_REGION" --only-show-errors
    rm -rf "$(dirname "$archive")"

    local build_id
    build_id=$(aws codebuild start-build \
        --project-name "$project" \
        --source-location-override "${bucket}/${key}" \
        --environment-variables-override "name=IMAGE_TAG,value=${image_tag},type=PLAINTEXT" \
        --region "$AWS_REGION" \
        --query 'build.id' \
        --output text)
    echo "   CodeBuild build started: ${build_id}"

    local waited=0 status phase
    while true; do
        read -r status phase < <(aws codebuild batch-get-builds \
            --ids "$build_id" \
            --region "$AWS_REGION" \
            --query 'builds[0].[buildStatus,currentPhase]' \
            --output text) || fail "Could not read the status of CodeBuild build ${build_id}."
        [ "$status" != "IN_PROGRESS" ] && break
        if [ "$waited" -ge "$BUILD_TIMEOUT_SECONDS" ]; then
            fail "CodeBuild build ${build_id} did not finish within ${BUILD_TIMEOUT_SECONDS}s (last phase: ${phase})."
        fi
        echo "   ... ${phase} (${waited}s)"
        sleep "$POLL_SECONDS"
        waited=$((waited + POLL_SECONDS))
    done

    if [ "$status" != "SUCCEEDED" ]; then
        echo "   Failed phases:" >&2
        aws codebuild batch-get-builds --ids "$build_id" --region "$AWS_REGION" \
            --query 'builds[0].phases[?phaseStatus!=`SUCCEEDED` && phaseStatus!=null].[phaseType,phaseStatus,contexts[0].message]' \
            --output text >&2 || true
        echo "   Logs: log group /aws/codebuild/${project}, stream ${build_id#*:}" >&2
        fail "CodeBuild build ${build_id} finished with status ${status}."
    fi
    echo "   CodeBuild build succeeded"
}

case "${1:-}" in
    build)
        [ "$#" -eq 2 ] || fail "Usage: $0 build <image-tag>"
        build "$2"
        ;;
    package)
        [ "$#" -eq 3 ] || fail "Usage: $0 package <ui-dir> <output-zip>"
        package_ui "$2" "$3"
        ;;
    *)
        fail "Usage: $0 build <image-tag> | package <ui-dir> <output-zip>"
        ;;
esac
