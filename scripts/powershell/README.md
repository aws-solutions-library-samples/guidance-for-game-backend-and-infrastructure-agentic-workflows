# Game Agent PowerShell Module

PowerShell module for deploying and managing Game Agent on AWS.

## 🎯 Features

- ✅ Deploy complete infrastructure to AWS
- ✅ Start/stop development environment
- ✅ Check deployment status
- ✅ Teardown all resources
- ✅ Cross-platform (Windows, Linux, macOS)
- ✅ Progress bars and colored output
- ✅ Comprehensive error handling

## 📋 Prerequisites

### Required

- **PowerShell 7.0+** - [Download](https://github.com/PowerShell/PowerShell/releases)
- **AWS CLI v2** - Install with:
  ```powershell
  # Windows
  winget install Amazon.AWSCLI
  # macOS
  brew install awscli
  # Linux
  curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" && unzip awscliv2.zip && sudo ./aws/install
  ```
- **AWS Credentials** - Configure with:
  ```powershell
  aws configure
  # Or set a named profile (replace <your-profile> with your profile name)
  aws configure --profile <your-profile>
  ```

### For Development

- **uv** (Python package manager) - [Installation](https://docs.astral.sh/uv/getting-started/installation/)
  ```powershell
  # Windows
  powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
  ```
- **Node.js 18+** - [Download](https://nodejs.org/)

### For Testing

- **Pester 5.x** (PowerShell testing framework) — auto-installed by `Test-GameAgentUnit` and `test-powershell.sh` if missing. To install manually:
  ```powershell
  Install-Module -Name Pester -Force -Scope CurrentUser -SkipPublisherCheck
  ```

## 🚀 Installation

### Option 1: Import Module Directly

```powershell
# Navigate to the module directory
cd scripts/powershell

# Import the module
Import-Module ./GameAgent.psd1

# Verify installation
Get-Command -Module GameAgent
```

### Option 2: Install to PowerShell Modules Path

```powershell
# Copy module to user modules directory
$modulePath = "$HOME\Documents\PowerShell\Modules\GameAgent"
New-Item -ItemType Directory -Path $modulePath -Force
Copy-Item -Path "scripts/powershell/*" -Destination $modulePath -Recurse

# Import module
Import-Module GameAgent

# Verify installation
Get-Module GameAgent
```

## 📖 Usage

### Deploy Infrastructure

```powershell
# Deploy with default settings (us-west-2)
Deploy-GameAgent

# Deploy to a specific region
Deploy-GameAgent -Region "us-east-1"

# Deploy with custom project name
Deploy-GameAgent -ProjectName "my-game-agent"

# Preview deployment without making changes
Deploy-GameAgent -WhatIf
```

### Start Development Environment

```powershell
# Start both backend and frontend
Start-GameAgentDev

# Start with custom ports
Start-GameAgentDev -BackendPort 8000 -FrontendPort 3001

# Start only backend
Start-GameAgentDev -BackendOnly

# Start only frontend
Start-GameAgentDev -FrontendOnly
```

### Check Status

```powershell
# Check deployment and development server status
Get-GameAgentStatus

# Check status in a specific region
Get-GameAgentStatus -Region "us-east-1"
```

### Stop Development Environment

```powershell
# Stop both servers
Stop-GameAgentDev

# Stop only backend
Stop-GameAgentDev -BackendOnly

# Stop only frontend
Stop-GameAgentDev -FrontendOnly
```

### Remove Infrastructure

```powershell
# Remove with confirmation prompt
Remove-GameAgent

# Force removal without confirmation
Remove-GameAgent -Force

# Remove from specific region
Remove-GameAgent -Region "us-east-1"
```

## 📚 Command Reference

### Deploy-GameAgent

Deploys Game Agent infrastructure to AWS.

**Parameters:**
- `-ProjectName` - Project name for resource naming (default: "game-agent")
- `-Region` - AWS region (default: "us-west-2")
- `-SkipKnowledgeBases` - Skip KB deployment
- `-WhatIf` - Preview without deploying

**Example:**
```powershell
Deploy-GameAgent -ProjectName "prod-game-agent" -Region "us-west-2"
```

### Start-GameAgentDev

Starts development servers in background jobs.

**Parameters:**
- `-BackendPort` - Backend port (default: 8080)
- `-FrontendPort` - Frontend port (default: 3000)
- `-BackendOnly` - Start only backend
- `-FrontendOnly` - Start only frontend

**Example:**
```powershell
Start-GameAgentDev -BackendPort 8000
```

### Stop-GameAgentDev

Stops development servers.

**Parameters:**
- `-BackendOnly` - Stop only backend
- `-FrontendOnly` - Stop only frontend

**Example:**
```powershell
Stop-GameAgentDev
```

### Get-GameAgentStatus

Gets deployment and server status.

**Parameters:**
- `-ProjectName` - Project name (default: "game-agent")
- `-Region` - AWS region (default: "us-west-2")

**Example:**
```powershell
Get-GameAgentStatus -Region "us-east-1"
```

### Remove-GameAgent

Removes all Game Agent resources.

**Parameters:**
- `-ProjectName` - Project name (default: "game-agent")
- `-Region` - AWS region (default: "us-west-2")
- `-Force` - Skip confirmation

**Example:**
```powershell
Remove-GameAgent -Force
```

### Add-GameAgentAdmin

Creates an admin user in the Cognito user pool. Prompts interactively for email/password if omitted.

**Parameters:**
- `-Email` - User email address
- `-Password` - Password (min 8 chars, mixed case, number, symbol)
- `-Profile` - AWS CLI profile
- `-Region` - AWS region (default: "us-west-2")

**Example:**
```powershell
Add-GameAgentAdmin -Email admin@example.com -Profile <your-profile>
```

### Remove-GameAgentAdmin

Deletes a user from the Cognito user pool. Lists all users if `-Email` is omitted.

**Parameters:**
- `-Email` - Email of user to delete
- `-All` - Delete all users
- `-Profile` - AWS CLI profile
- `-Region` - AWS region (default: "us-west-2")

**Example:**
```powershell
Remove-GameAgentAdmin -Email admin@example.com -Profile <your-profile>
```

### Test-GameAgentUnit

Runs backend, frontend, and PowerShell module unit tests. Auto-installs Pester if missing.

**Example:**
```powershell
Test-GameAgentUnit
```

### Test-GameAgentFull

Runs the full smart test suite — auto-detects deployed stack, localhost, or unit-only mode.

**Parameters:**
- `-Profile` - AWS CLI profile
- `-Region` - AWS region (default: "us-west-2")

**Example:**
```powershell
Test-GameAgentFull -Profile <your-profile>
```

## 🧪 Testing

Pester tests live in `Tests/` and cover the private helper functions and module exports.

```powershell
# Run PowerShell module tests only
Invoke-Pester scripts/powershell/Tests -Output Detailed

# Run all unit tests (backend + frontend + PowerShell module)
Test-GameAgentUnit
```

From bash (macOS/Linux/CI):
```bash
./test-powershell.sh
```

Both `Test-GameAgentUnit` and `test-powershell.sh` auto-install Pester if it's missing.

## 🔧 Troubleshooting

### AWS Credentials Not Found

Configure AWS credentials (interactive — run separately):
```powershell
aws configure
```

Or use a named profile:
```powershell
aws configure --profile <your-profile>
```

### Module Not Found

```powershell
# Check if module is in the right location
Get-Module -ListAvailable GameAgent

# Import module explicitly
Import-Module ./scripts/powershell/GameAgent.psd1 -Force
```

### Port Already in Use

```powershell
# Check what's using the port
Get-NetTCPConnection -LocalPort 8080

# Stop existing GameAgent servers
Stop-GameAgentDev

# Or use different ports
Start-GameAgentDev -BackendPort 8000 -FrontendPort 3001
```

### Development Server Won't Start

```powershell
# Check if uv is installed
Get-Command uv

# Check if npm is installed
Get-Command npm

# Check dev server logs
Get-Content logs/dev-agentcore.log -Tail 50
Get-Content logs/dev-frontend.log -Tail 50
```

## 🎨 Features

### Progress Bars

All long-running operations show progress bars:
```
Deploying Game Agent
Stack 2 of 5: game-agent-security
[████████████████████░░░░░░░░] 60%
```

### Colored Output

- 🚀 Info messages (Cyan)
- ✅ Success messages (Green)
- ⚠️ Warning messages (Yellow)
- ❌ Error messages (Red)

### Error Handling

Comprehensive error handling with helpful messages:
```powershell
# AWS credentials not configured
Please configure AWS credentials using 'aws configure'
```

## 🔗 Related Commands

### View Dev Server Logs

```powershell
# View backend logs
Get-Content logs/dev-agentcore.log -Tail 50

# View frontend logs
Get-Content logs/dev-frontend.log -Tail 50
```

### AWS Stack Operations

```powershell
# List all GameAgent stacks
aws cloudformation list-stacks --region us-west-2 --query "StackSummaries[?starts_with(StackName,'game-agent-')].[StackName,StackStatus]" --output table

# Get stack outputs
aws cloudformation describe-stacks --stack-name game-agent-infrastructure --region us-west-2 --query "Stacks[0].Outputs" --output table
```

## 📝 Examples

### Complete Workflow

```powershell
# 1. Import module
Import-Module ./scripts/powershell/GameAgent.psd1

# 2. Check status
Get-GameAgentStatus

# 3. Deploy infrastructure
Deploy-GameAgent -Region "us-west-2"

# 4. Start development
Start-GameAgentDev

# 5. Open browser
Start-Process "http://localhost:3000"

# 6. Stop development when done
Stop-GameAgentDev

# 7. Remove infrastructure (optional)
Remove-GameAgent -Force
```

### Multi-Region Deployment

```powershell
# Deploy to multiple regions
$regions = @("us-west-2", "us-east-1", "eu-west-1")

foreach ($region in $regions) {
    Deploy-GameAgent -ProjectName "game-agent-$region" -Region $region
}
```

## 🆘 Support

For issues or questions:
1. Check the [main README](../../README.md)
2. View logs: `Get-Content logs/dev-agentcore.log -Tail 50`
3. Check AWS Console for stack events
4. Open an issue on GitHub

## 📄 License

See [LICENSE](../../LICENSE) file.
