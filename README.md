# Investigation Orchestrator

**Multi-Client AWS DevOps Agent Monitoring and Incident Response Automation**

## Overview

Investigation Orchestrator is an event-driven architecture that monitors AWS DevOps Agent investigations across multiple client accounts, providing centralized visibility, intelligent routing, and proactive incident response coordination. The system aggregates investigation summaries from client accounts while maintaining strict data isolation and security boundaries.

## Architecture Principles

- **Security First**: Client data never crosses account boundaries - only metadata and links
- **Event-Driven**: Asynchronous, loosely coupled components using EventBridge
- **Hybrid Intelligence**: 90% simple routing (Lambda), 10% complex analysis (Bedrock Agent)
- **Cost-Optimized**: Pay-per-use serverless architecture (~$75-95/month for 10 clients)
- **Scalable**: Designed to handle 100+ client accounts without architectural changes

## Architecture Overview

[image](docs/architecture.drawio.png)

```
┌─────────────────────────────────────────────────────┐
│         Client Account A (123456789012)             │
│  ┌──────────────┐      ┌──────────────────────┐    │
│  │ AWS DevOps   │      │ Investigation        │    │
│  │ Agent        │──────▶ Watcher Lambda       │    │
│  │              │      │ (Polls every 5 min)  │    │
│  └──────────────┘      └──────────┬───────────┘    │
│                                    │                 │
│                                    ▼                 │
│                         ┌────────────────────────┐  │
│                         │ Event Formatter       │  │
│                         │ - Extracts summary    │  │
│                         │ - Redacts sensitive   │  │
│                         │ - Generates links     │  │
│                         └──────────┬────────────┘  │
└────────────────────────────────────┼────────────────┘
                                     │
                    EventBridge PutEvents (Summary Only)
                                     │
┌────────────────────────────────────▼────────────────┐
│      Central Monitoring Account (999999999999)      │
│                                                      │
│  ┌──────────────────────────────────────────────┐  │
│  │ EventBridge Event Bus                        │  │
│  │ - Receives events from all clients           │  │
│  │ - Routes based on severity/patterns          │  │
│  └───┬──────────────────────────────────┬───────┘  │
│      │                                   │           │
│      ▼                                   ▼           │
│  ┌──────────────────┐      ┌─────────────────────┐ │
│  │ Simple Routing   │      │ Pattern Detection   │ │
│  │ Lambda (90%)     │      │ Lambda (10%)        │ │
│  │ - HIGH → Page    │      │ - Uses Bedrock      │ │
│  │ - MED → Ticket   │      │ - Correlates events │ │
│  └────────┬─────────┘      └──────────┬──────────┘ │
│           │                           │             │
│           ▼                           ▼             │
│  ┌──────────────────────────────────────────────┐  │
│  │ DynamoDB: Investigation Tracker              │  │
│  │ - Active investigations                      │  │
│  │ - Historical patterns                        │  │
│  └──────────────────────────────────────────────┘  │
│                                                      │
│  ┌──────────────────────────────────────────────┐  │
│  │ CloudWatch Dashboard                         │  │
│  │ - Client health overview                     │  │
│  │ - MTTR trends                                │  │
│  │ - Investigation outcomes                     │  │
│  └──────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────┘
```

## Key Components

### Client Account Stack (Per Client)

| Component | Purpose | Trigger |
|-----------|---------|---------|
| **AWS DevOps Agent** | Investigates incidents in client infrastructure | CloudWatch alarms, tickets |
| **Investigation Watcher Lambda** | Polls DevOps Agent for completed investigations | EventBridge Schedule (5 min) |
| **Event Formatter Lambda** | Extracts summary, redacts sensitive data, generates signed links | Watcher Lambda invocation |

**Key Outputs:**
- Investigation summary (3-10 KB)
- Links to DevOps Agent web app
- Metadata for routing

**Security:**
- No raw logs cross account boundary
- Only metadata and resource counts
- IAM role with minimal permissions

---

### Central Monitoring Stack

| Component | Purpose | Cost |
|-----------|---------|------|
| **EventBridge Event Bus** | Receives events from all clients | $1/million events |
| **EventBridge Rules** | Routes based on severity/patterns | Included |
| **Simple Routing Lambda** | Handles 90% of cases (page/ticket) | $0.20/million requests |
| **Pattern Detection Lambda** | Analyzes complex scenarios with Bedrock Agent | $3-5/invocation |
| **DynamoDB Table** | Tracks investigations, historical patterns | $0.25/GB + on-demand |
| **CloudWatch Dashboard** | Operational visibility across clients | $3/dashboard |

---

## Data Flow

### 1. Investigation Complete
```
Client DevOps Agent completes investigation
→ Updates internal state
→ Posts to Slack (client-specific channel)
```

### 2. Event Detection
```
Investigation Watcher Lambda (runs every 5 min)
→ Queries DevOps Agent status
→ Detects completed investigations
→ Invokes Event Formatter Lambda
```

### 3. Event Formatting
```
Event Formatter Lambda
→ Extracts summary from investigation
→ Redacts sensitive data (IPs, logs, secrets)
→ Generates signed URLs to DevOps Agent
→ Sends to EventBridge in central account
```

### 4. Event Routing
```
EventBridge Event Bus (central account)
→ Applies rules based on event attributes
→ Routes to appropriate target(s)

If severity = CRITICAL or HIGH:
  → Simple Routing Lambda → PagerDuty API

If severity = MEDIUM:
  → Simple Routing Lambda → Jira API

If pattern detected (multiple clients, same root cause):
  → Pattern Detection Lambda → Bedrock Agent → Senior engineer alert
```

### 5. Storage & Visibility
```
All events stored in DynamoDB
→ CloudWatch Dashboard updated
→ Metrics published (MTTR, investigation count, outcomes)
```

---

## Event Schema

```json
{
  "event_type": "investigation_completed",
  "investigation_id": "inv-abc123",
  "client_account_id": "123456789012",
  "client_name": "Acme Corp",
  "timestamp": "2025-12-18T10:30:00Z",
  "severity": "HIGH",
  "status": "ROOT_CAUSE_FOUND",
  
  "summary": {
    "affected_resources": ["i-123", "db-prod-01"],
    "resource_types": ["EC2", "RDS"],
    "duration_minutes": 45,
    "root_cause_category": "resource_exhaustion",
    "root_cause_brief": "RDS connection pool exhausted",
    "mitigation_status": "plan_generated"
  },
  
  "links": {
    "devops_agent_investigation": "https://...",
    "cloudwatch_logs": "https://...",
    "affected_application": "https://..."
  },
  
  "tags": {
    "environment": "production",
    "application": "web-api",
    "on_call_team": "platform-team"
  }
}
```

**Size:** ~3-10 KB (well under 256 KB EventBridge limit)

---

## Security Model

### Cross-Account Access

**Client → Central:**
```
Client Lambda execution role has permission to:
- events:PutEvents on central EventBridge bus
- ONLY for the specific event bus ARN
- Scoped by aws:PrincipalOrgID condition
```

**Central → Client:**
```
Central account engineers can assume role to:
- View DevOps Agent web app
- Access CloudWatch Logs (read-only)
- Query investigation details
- Time-limited sessions (1 hour)
```

### Data Classification

| Data Type | Crosses Account Boundary? | Justification |
|-----------|--------------------------|---------------|
| Investigation ID | Yes | Required for linking |
| Resource counts | Yes | Metadata only |
| Root cause category | Yes | High-level classification |
| Root cause summary | Yes | Redacted, max 200 chars |
| CloudWatch logs | No | Contains PII/sensitive data |
| Metrics/traces | No | Can contain IP addresses |
| Database connection strings | No | Secrets |
| Application code | No | Proprietary |

---

## Cost Analysis

### Per Client (10 clients example)

| Component | Cost/Month |
|-----------|-----------|
| Investigation Watcher Lambda (8,640 invocations @ 5 min) | $1.73 |
| Event Formatter Lambda (50 investigations) | $0.10 |
| EventBridge PutEvents (50 events) | $0.00005 |
| **Total per client** | **~$2/month** |

### Central Account

| Component | Cost/Month |
|-----------|-----------|
| EventBridge Event Bus (500 events/month) | $0.50 |
| Simple Routing Lambda (450 invocations) | $0.09 |
| Pattern Detection Lambda + Bedrock (50 invocations) | $30-50 |
| DynamoDB (1 GB storage, on-demand) | $1.25 |
| CloudWatch Dashboard | $3.00 |
| Step Functions (optional) | $2.50 |
| **Total central** | **~$37-57/month** |

### Grand Total (10 clients)
```
10 clients × $2 + $47 (central) = ~$67/month
With Bedrock Agent: ~$87/month
```

**Compared to alternatives:**
- Hiring another engineer: $10,000+/month
- ServiceNow multi-instance: $1,200-2,000/month
- PagerDuty enterprise: $500-1,000/month

---

## Deployment Prerequisites

### AWS Accounts Required
- 1 Central Monitoring Account
- N Client Accounts (one per client)

### AWS Services Required
- AWS DevOps Agent (preview, us-east-1 only)
- AWS CDK v2.x
- Python 3.11+
- AWS CLI v2

### IAM Permissions Needed

**Central Account:**
- Create EventBridge event bus
- Create Lambda functions
- Create DynamoDB tables
- Create IAM roles
- Create CloudWatch dashboards

**Client Accounts:**
- Create Lambda functions
- Create EventBridge schedules
- Create IAM roles
- events:PutEvents to central account

---

## Quick Start

### 1. Clone Repository
```bash
git clone <repository>
cd investigation-orchestrator
```

### 2. Install Dependencies
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure Clients
```bash
# Edit config files
vim config/central_account.yaml
vim config/clients/client_a.yaml
```

### 4. Deploy Central Stack
```bash
export AWS_PROFILE=central-account
cd stacks
cdk deploy CentralMonitoringStack
```

### 5. Deploy Client Stack
```bash
export AWS_PROFILE=client-a-account
cdk deploy ClientInvestigationStack \
  --context clientName=ClientA \
  --context centralEventBusArn=<from-central-stack>
```

### 6. Verify Deployment
```bash
# Check EventBridge is receiving events
aws events put-events --entries file://test-event.json

# View CloudWatch Dashboard
aws cloudwatch get-dashboard --dashboard-name InvestigationOrchestrator
```

---

## Configuration

### Client Configuration (client_a.yaml)
```yaml
client_name: "Acme Corporation"
client_account_id: "123456789012"
environment: "production"

investigation_watcher:
  schedule_rate: "rate(5 minutes)"
  timeout_seconds: 60

devops_agent:
  agent_space_id: "space-abc123"
  region: "us-east-1"

tags:
  cost_center: "engineering"
  on_call_team: "platform-team"
  tier: "enterprise"
```

### Central Configuration (central_account.yaml)
```yaml
central_account_id: "999999999999"
organization_id: "o-xxxxxxxxxx"

event_bus:
  name: "client-investigations"
  retention_days: 90

routing_rules:
  high_severity:
    severities: ["CRITICAL", "HIGH"]
    actions: ["page_engineer", "create_ticket"]
  
  medium_severity:
    severities: ["MEDIUM"]
    actions: ["create_ticket"]
  
  pattern_detection:
    enabled: true
    min_events: 3
    time_window_minutes: 60

bedrock_agent:
  model_id: "anthropic.claude-sonnet-4-20250514"
  region: "us-east-1"

integrations:
  pagerduty:
    api_key_secret: "pagerduty/api-key"
    service_id: "PXXXXXX"
  
  jira:
    api_key_secret: "jira/api-token"
    project_key: "OPS"
    issue_type: "Incident"
```

---

## Monitoring & Operations

### Key Metrics

**EventBridge Metrics:**
- `Invocations` - Total events received
- `FailedInvocations` - Events that failed to deliver
- `ThrottledRules` - Rules hitting limits

**Lambda Metrics:**
- `Duration` - Processing time
- `Errors` - Failed invocations
- `Throttles` - Concurrency limits hit

**DynamoDB Metrics:**
- `ConsumedReadCapacityUnits`
- `ConsumedWriteCapacityUnits`
- `UserErrors` - Throttling or validation errors

### Alarms

| Alarm | Threshold | Action |
|-------|-----------|--------|
| High error rate | >5% in 5 min | Page on-call |
| EventBridge throttling | >0 in 15 min | Alert engineering |
| Large event size | >200 KB | Investigate sender |
| Pattern detector failures | >3 in 1 hour | Check Bedrock Agent |

### Troubleshooting

**Event not appearing in central account:**
1. Check client Lambda CloudWatch Logs
2. Verify IAM permissions for events:PutEvents
3. Check EventBridge metrics for FailedInvocations
4. Validate event schema matches expected format

**Pattern detection not triggering:**
1. Check DynamoDB has recent investigations
2. Verify Bedrock Agent is configured
3. Check Lambda has permissions to invoke Bedrock
4. Review CloudWatch Logs for Pattern Detection Lambda

**False positive investigations:**
1. Review DevOps Agent configuration
2. Adjust CloudWatch alarm thresholds
3. Add filters to Investigation Watcher
4. Update event schema validation

---

## Roadmap

### Phase 1: MVP (Current)
- EventBridge cross-account event bus
- Simple routing (HIGH → page, MEDIUM → ticket)
- Basic DynamoDB tracking
- CloudWatch Dashboard

### Phase 2: Intelligence
- Bedrock Agent integration for pattern detection
- Multi-client correlation analysis
- Automated recommendations

### Phase 3: Automation
- Auto-remediation for approved scenarios
- Step Functions orchestration
- GitHub PR generation for infrastructure changes

### Phase 4: Advanced Features
- Machine learning for MTTR prediction
- Cost impact analysis
- SLA tracking and reporting

---

## Contributing

### Development Setup
```bash
# Install dev dependencies
pip install -r requirements-dev.txt

# Run tests
pytest tests/

# Run linting
pylint lambda/ stacks/
black --check .

# Type checking
mypy lambda/ stacks/
```

### Testing
```bash
# Unit tests
pytest tests/unit/

# Integration tests (requires AWS credentials)
pytest tests/integration/

# CDK synthesis test
cdk synth --all
```

---

## Support

### Documentation
- Architecture diagram: `docs/architecture.drawio`
- API reference: `docs/api.md`
- Runbooks: `docs/runbooks/`

### Contact
- On-call engineering: `oncall@example.com`
- Slack: `#investigation-orchestrator`
- Jira project: `OPS`

---

## License

Internal use only - Proprietary

---

## Acknowledgments

Built with:
- AWS CDK
- AWS EventBridge
- AWS Lambda
- Amazon Bedrock
- AWS DevOps Agent (preview)