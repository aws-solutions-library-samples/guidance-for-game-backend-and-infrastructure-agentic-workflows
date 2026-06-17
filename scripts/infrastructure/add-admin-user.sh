#!/bin/bash
set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Resolve AWS profile from environment or ui/.env.local (matches scripts/deploy.sh).
# An explicitly set AWS_PROFILE always wins; otherwise fall back to ui/.env.local.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
if [ -z "${AWS_PROFILE:-}" ] && [ -f "$PROJECT_ROOT/ui/.env.local" ]; then
    _profile=$(grep '^AWS_PROFILE=' "$PROJECT_ROOT/ui/.env.local" | cut -d= -f2 | tr -d '[:space:]')
    [ -n "$_profile" ] && export AWS_PROFILE="$_profile"
fi

echo -e "${BLUE}=== Add Admin User to Cognito ===${NC}\n"

# Get User Pool ID from CloudFormation
echo "Fetching User Pool ID..."
USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name game-agent-infrastructure \
  --query 'Stacks[0].Outputs[?OutputKey==`UserPoolId`].OutputValue' \
  --output text)

if [ -z "$USER_POOL_ID" ]; then
  echo -e "${RED}Error: Could not find User Pool ID. Is the stack deployed?${NC}"
  exit 1
fi

echo -e "${GREEN}User Pool ID: $USER_POOL_ID${NC}\n"

# Prompt for email
read -p "Enter your email address: " EMAIL

if [ -z "$EMAIL" ]; then
  echo -e "${RED}Error: Email is required${NC}"
  exit 1
fi

# Prompt for password
read -sp "Enter your password (min 8 chars, uppercase, lowercase, number, symbol): " PASSWORD
echo

if [ -z "$PASSWORD" ]; then
  echo -e "${RED}Error: Password is required${NC}"
  exit 1
fi

# Create user
echo -e "\n${BLUE}Creating user...${NC}"
aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" \
  --username "$EMAIL" \
  --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true \
  --message-action SUPPRESS

# Set permanent password
echo -e "${BLUE}Setting password...${NC}"
aws cognito-idp admin-set-user-password \
  --user-pool-id "$USER_POOL_ID" \
  --username "$EMAIL" \
  --password "$PASSWORD" \
  --permanent

# Add user to admin group
echo -e "${BLUE}Adding user to admin group...${NC}"
aws cognito-idp admin-add-user-to-group \
  --user-pool-id "$USER_POOL_ID" \
  --username "$EMAIL" \
  --group-name admin

echo -e "\n${GREEN}✓ Admin user created successfully!${NC}"
echo -e "\nYou can now log in with:"
echo -e "  Email: ${BLUE}$EMAIL${NC}"
echo -e "  Password: ${BLUE}(the one you just entered)${NC}"
