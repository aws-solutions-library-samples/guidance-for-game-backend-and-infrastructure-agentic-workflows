#!/bin/bash
set -e

# Colors for output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${BLUE}=== Purge All Cognito Users ===${NC}\n"

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

# List all users
echo "Fetching all users..."
USERS=$(aws cognito-idp list-users \
  --user-pool-id "$USER_POOL_ID" \
  --query 'Users[].Username' \
  --output text)

if [ -z "$USERS" ]; then
  echo -e "${YELLOW}No users found in the user pool.${NC}"
  exit 0
fi

# Show users
echo -e "${BLUE}Found users:${NC}"
for user in $USERS; do
  echo "  - $user"
done
echo ""

# Confirm deletion
read -p "Delete ALL users? (yes/no): " CONFIRM

if [ "$CONFIRM" != "yes" ]; then
  echo -e "${YELLOW}Cancelled. No users deleted.${NC}"
  exit 0
fi

# Delete each user
echo -e "\n${BLUE}Deleting users...${NC}"
for user in $USERS; do
  echo "  Deleting: $user"
  aws cognito-idp admin-delete-user \
    --user-pool-id "$USER_POOL_ID" \
    --username "$user"
done

echo -e "\n${GREEN}✓ All users deleted successfully!${NC}"
echo -e "\nYou can now create a new admin user with:"
echo -e "  ${BLUE}./scripts/infrastructure/add-admin-user.sh${NC}"
