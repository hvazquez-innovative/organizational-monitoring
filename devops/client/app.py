#!/usr/bin/env python3
"""
Investigation Orchestrator - Client Account

Deploys client-side infrastructure:
- Investigation monitor Lambda (polls DevOps Agent, formats, and sends to central)
- EventBridge schedule (triggers monitor every 5 min)
- IAM roles for cross-account access
"""

import os
import yaml
from aws_cdk import (
    Stack,
    App,
    Duration,
    CfnOutput,
    aws_lambda,
    aws_iam,
    aws_events,
    aws_events_targets,
    aws_logs,
)
from constructs import Construct


class ClientInvestigationStack(Stack):
    """Client account infrastructure for investigation monitoring"""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        client_name: str,
        client_config: dict,
        environment_name: str,
        **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        account = Stack.of(self).account
        region = Stack.of(self).region

        central_event_bus_arn = client_config["central_event_bus_arn"]
        devops_agent_config = client_config["devops_agent"]

        # Investigation monitor Lambda - handles polling + formatting + sending
        investigation_monitor_lambda = aws_lambda.Function(
            self,
            "InvestigationMonitorLambda",
            function_name=f"investigation-monitor-{client_name.lower().replace(' ', '-')}-{environment_name}",
            description=f"Monitors DevOps Agent investigations for {client_name} and sends to central account",
            runtime=aws_lambda.Runtime.PYTHON_3_11,
            handler="index.handler",
            code=aws_lambda.Code.from_asset("../lambda/investigation_monitor"),
            timeout=Duration.seconds(60),
            memory_size=256,
            environment={
                "CLIENT_NAME": client_name,
                "CLIENT_ACCOUNT_ID": client_config["client_account_id"],
                "CENTRAL_EVENT_BUS_ARN": central_event_bus_arn,
                "DEVOPS_AGENT_SPACE_ID": devops_agent_config.get("agent_space_id", ""),
                "DEVOPS_AGENT_REGION": devops_agent_config.get("region", "us-east-1"),
                "STATE_PARAMETER_NAME": f"/investigation-orchestrator/{client_name}/last-processed-investigation",
                "ENVIRONMENT": environment_name,
                "TAGS": str(client_config.get("tags", {}))
            },
            tracing=aws_lambda.Tracing.ACTIVE,
            log_retention=aws_logs.RetentionDays.ONE_MONTH,
        )

        # Grant permission to send events to central EventBridge
        investigation_monitor_lambda.add_to_role_policy(
            aws_iam.PolicyStatement(
                effect=aws_iam.Effect.ALLOW,
                actions=["events:PutEvents"],
                resources=[central_event_bus_arn]
            )
        )

        # Grant permission to read/write state to SSM Parameter Store
        investigation_monitor_lambda.add_to_role_policy(
            aws_iam.PolicyStatement(
                effect=aws_iam.Effect.ALLOW,
                actions=[
                    "ssm:GetParameter",
                    "ssm:PutParameter"
                ],
                resources=[
                    f"arn:aws:ssm:{region}:{account}:parameter/investigation-orchestrator/{client_name}/*"
                ]
            )
        )

        # Grant permission to access DevOps Agent (if API is available)
        investigation_monitor_lambda.add_to_role_policy(
            aws_iam.PolicyStatement(
                effect=aws_iam.Effect.ALLOW,
                actions=[
                    "devops-agent:GetInvestigation",
                    "devops-agent:ListInvestigations",
                    "devops-agent:DescribeAgentSpace"
                ],
                resources=[
                    f"arn:aws:devops-agent:{devops_agent_config.get('region', 'us-east-1')}:{account}:agent-space/{devops_agent_config.get('agent_space_id', '*')}"
                ]
            )
        )

        # Grant CloudWatch Logs read access (to parse DevOps Agent activity)
        investigation_monitor_lambda.add_to_role_policy(
            aws_iam.PolicyStatement(
                effect=aws_iam.Effect.ALLOW,
                actions=[
                    "logs:DescribeLogGroups",
                    "logs:DescribeLogStreams",
                    "logs:FilterLogEvents",
                    "logs:GetLogEvents"
                ],
                resources=[
                    f"arn:aws:logs:{region}:{account}:log-group:/aws/devops-agent/*"
                ]
            )
        )

        # Grant X-Ray permissions
        investigation_monitor_lambda.add_to_role_policy(
            aws_iam.PolicyStatement(
                effect=aws_iam.Effect.ALLOW,
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords"
                ],
                resources=["*"]
            )
        )

        # EventBridge schedule to trigger investigation monitor
        schedule_rate = client_config.get("investigation_monitor", {}).get(
            "schedule_rate", "rate(5 minutes)"
        )

        rule = aws_events.Rule(
            self,
            "InvestigationMonitorSchedule",
            rule_name=f"investigation-monitor-schedule-{client_name.lower().replace(' ', '-')}-{environment_name}",
            description=f"Triggers investigation monitor for {client_name} every 5 minutes",
            schedule=aws_events.Schedule.expression(schedule_rate)
        )

        rule.add_target(aws_events_targets.LambdaFunction(investigation_monitor_lambda))

        # CloudFormation outputs
        CfnOutput(
            self,
            "InvestigationMonitorFunctionName",
            value=investigation_monitor_lambda.function_name,
            description="Investigation monitor Lambda function name"
        )

        CfnOutput(
            self,
            "ClientName",
            value=client_name,
            description="Client name for this deployment"
        )

        CfnOutput(
            self,
            "CentralEventBusArn",
            value=central_event_bus_arn,
            description="Central EventBridge bus ARN this client sends to"
        )


# CDK App
app = App()

# Get configuration
client_name = app.node.try_get_context("clientName") or os.getenv("CLIENT_NAME")
config_file = app.node.try_get_context("configFile") or os.getenv("CONFIG_FILE")
environment_name = app.node.try_get_context("environment") or os.getenv("ENVIRONMENT", "dev")

if not client_name:
    raise ValueError("clientName must be provided via context or CLIENT_NAME env var")

# Load client configuration
if config_file:
    with open(config_file, 'r') as f:
        client_config = yaml.safe_load(f)
else:
    # Load from default location
    config_path = f"../config/clients/{client_name.lower().replace(' ', '_')}.yaml"
    try:
        with open(config_path, 'r') as f:
            client_config = yaml.safe_load(f)
    except FileNotFoundError:
        raise ValueError(f"Config file not found: {config_path}. Provide via --context configFile=path/to/config.yaml")

# Validate required fields
required_fields = ["client_account_id", "central_event_bus_arn", "devops_agent"]
for field in required_fields:
    if field not in client_config:
        raise ValueError(f"Required field '{field}' missing from client config")

# Deploy client investigation stack
ClientInvestigationStack(
    app,
    f"InvestigationOrchestrator-Client-{client_name}-{environment_name}",
    stack_name=f"investigation-orchestrator-client-{client_name.lower().replace(' ', '-')}-{environment_name}",
    description=f"Client-side investigation monitoring for {client_name}",
    client_name=client_name,
    client_config=client_config,
    environment_name=environment_name,
    env={
        "account": client_config["client_account_id"],
        "region": client_config.get("region", "us-east-1")
    },
    tags={
        "Project": "InvestigationOrchestrator",
        "Component": "Client",
        "Client": client_name,
        "Environment": environment_name,
        "ManagedBy": "CDK"
    }
)

app.synth()
