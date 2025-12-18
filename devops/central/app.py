#!/usr/bin/env python3
"""
Investigation Orchestrator - Central Account

Deploys central monitoring infrastructure:
- EventBridge event bus
- Simple routing Lambda
- Pattern detection Lambda (with Bedrock)
- DynamoDB investigation tracker
- CloudWatch dashboards
"""

import os
from aws_cdk import (
    Stack,
    App,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_events,
    aws_events_targets,
    aws_lambda,
    aws_dynamodb,
    aws_iam,
    aws_logs,
    aws_sns,
    aws_sns_subscriptions,
    aws_cloudwatch,
    aws_cloudwatch_actions,
    aws_sqs,
)
from constructs import Construct

organization_id = os.getenv("ORGANIZATION_ID", "o-1234567890")
environment_name = os.getenv("ENVIRONMENT", "dev")
central_account_id = os.getenv("CENTRAL_ACCOUNT_ID", "123456789012")

class CentralMonitoringStack(Stack):
    """Central account infrastructure for multi-client investigation monitoring"""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        account = Stack.of(self).account
        region = Stack.of(self).region

        # EventBridge event bus for client events
        event_bus = aws_events.EventBus(
            self,
            "ClientInvestigationsEventBus",
            event_bus_name=f"client-investigations-{environment_name}",
            description="Receives investigation events from all client accounts"
        )

        # Add resource policy to allow events from organization
        event_bus.add_to_resource_policy(
            aws_iam.PolicyStatement(
                effect=aws_iam.Effect.ALLOW,
                principals=[aws_iam.AnyPrincipal()],
                actions=["events:PutEvents"],
                resources=[event_bus.event_bus_arn],
                conditions={
                    "StringEquals": {
                        "aws:PrincipalOrgID": organization_id
                    }
                }
            )
        )

        # Archive for replay/debugging (90 day retention)
        aws_events.Archive(
            self,
            "InvestigationEventsArchive",
            archive_name=f"investigation-events-{environment_name}",
            source_event_bus=event_bus,
            description="Archive of all investigation events for replay/debugging",
            retention=Duration.days(90),
            event_pattern=aws_events.EventPattern(
                source=["devops.investigation"]
            )
        )

        # DynamoDB table for investigation tracking
        investigations_table = aws_dynamodb.Table(
            self,
            "InvestigationTrackerTable",
            table_name=f"investigation-tracker-{environment_name}",
            partition_key=aws_dynamodb.Attribute(
                name="investigation_id",
                type=aws_dynamodb.AttributeType.STRING
            ),
            sort_key=aws_dynamodb.Attribute(
                name="timestamp",
                type=aws_dynamodb.AttributeType.STRING
            ),
            billing_mode=aws_dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.RETAIN,
            point_in_time_recovery=True,
            stream=aws_dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
        )

        # GSI for querying by client account
        investigations_table.add_global_secondary_index(
            index_name="ClientAccountIndex",
            partition_key=aws_dynamodb.Attribute(
                name="client_account_id",
                type=aws_dynamodb.AttributeType.STRING
            ),
            sort_key=aws_dynamodb.Attribute(
                name="timestamp",
                type=aws_dynamodb.AttributeType.STRING
            )
        )

        # GSI for querying by severity
        investigations_table.add_global_secondary_index(
            index_name="SeverityIndex",
            partition_key=aws_dynamodb.Attribute(
                name="severity",
                type=aws_dynamodb.AttributeType.STRING
            ),
            sort_key=aws_dynamodb.Attribute(
                name="timestamp",
                type=aws_dynamodb.AttributeType.STRING
            )
        )

        # SNS topic for critical alerts
        alert_topic = aws_sns.Topic(
            self,
            "CriticalAlertTopic",
            topic_name=f"investigation-critical-alerts-{environment_name}",
            display_name="Critical Investigation Alerts"
        )

        # Add email subscription if provided via context
        alert_email = self.node.try_get_context("alertEmail")
        if alert_email:
            alert_topic.add_subscription(
                aws_sns_subscriptions.EmailSubscription(alert_email)
            )

        # Simple routing Lambda (90% of events)
        simple_routing_lambda = aws_lambda.Function(
            self,
            "SimpleRoutingLambda",
            function_name=f"investigation-simple-routing-{environment_name}",
            description="Routes investigation events based on severity (90% of cases)",
            runtime=aws_lambda.Runtime.PYTHON_3_11,
            handler="index.handler",
            code=aws_lambda.Code.from_asset("../lambda/simple_routing"),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                "INVESTIGATIONS_TABLE": investigations_table.table_name,
                "ALERT_TOPIC_ARN": alert_topic.topic_arn,
                "ENVIRONMENT": environment_name,
                "PAGERDUTY_API_KEY_SECRET": self.node.try_get_context("pagerdutySecretName") or "",
                "JIRA_API_KEY_SECRET": self.node.try_get_context("jiraSecretName") or "",
            },
            tracing=aws_lambda.Tracing.ACTIVE,
            log_retention=aws_logs.RetentionDays.ONE_MONTH,
        )

        # Grant permissions for simple routing Lambda
        investigations_table.grant_write_data(simple_routing_lambda)
        alert_topic.grant_publish(simple_routing_lambda)
        simple_routing_lambda.add_to_role_policy(
            aws_iam.PolicyStatement(
                effect=aws_iam.Effect.ALLOW,
                actions=[
                    "secretsmanager:GetSecretValue",
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords"
                ],
                resources=[
                    f"arn:aws:secretsmanager:{region}:{account}:secret:pagerduty/*",
                    f"arn:aws:secretsmanager:{region}:{account}:secret:jira/*",
                    "*"  # X-Ray
                ]
            )
        )

        # Pattern detection Lambda (10% of events, uses Bedrock)
        pattern_detection_lambda = aws_lambda.Function(
            self,
            "PatternDetectionLambda",
            function_name=f"investigation-pattern-detection-{environment_name}",
            description="Analyzes patterns across investigations using Bedrock Agent",
            runtime=aws_lambda.Runtime.PYTHON_3_11,
            handler="index.handler",
            code=aws_lambda.Code.from_asset("../lambda/pattern_detector"),
            timeout=Duration.seconds(300),
            memory_size=512,
            environment={
                "INVESTIGATIONS_TABLE": investigations_table.table_name,
                "ALERT_TOPIC_ARN": alert_topic.topic_arn,
                "BEDROCK_MODEL_ID": "anthropic.claude-sonnet-4-20250514",
                "ENVIRONMENT": environment_name,
            },
            tracing=aws_lambda.Tracing.ACTIVE,
            log_retention=aws_logs.RetentionDays.ONE_MONTH,
        )

        # Grant permissions for pattern detection Lambda
        investigations_table.grant_read_write_data(pattern_detection_lambda)
        alert_topic.grant_publish(pattern_detection_lambda)
        pattern_detection_lambda.add_to_role_policy(
            aws_iam.PolicyStatement(
                effect=aws_iam.Effect.ALLOW,
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords"
                ],
                resources=[
                    f"arn:aws:bedrock:{region}::foundation-model/anthropic.claude-sonnet-4-20250514",
                    "*"  # X-Ray
                ]
            )
        )

        # EventBridge rules for routing
        
        # Rule 1: High/Critical severity → Simple routing + SNS
        high_severity_rule = aws_events.Rule(
            self,
            "HighSeverityRule",
            rule_name=f"investigation-high-severity-{environment_name}",
            description="Routes HIGH/CRITICAL investigations to simple routing and alerts",
            event_bus=event_bus,
            event_pattern=aws_events.EventPattern(
                source=["devops.investigation"],
                detail_type=["InvestigationCompleted"],
                detail={
                    "severity": ["CRITICAL", "HIGH"]
                }
            )
        )
        high_severity_rule.add_target(aws_events_targets.LambdaFunction(simple_routing_lambda))
        high_severity_rule.add_target(aws_events_targets.SnsTopic(alert_topic))

        # Rule 2: Medium severity → Simple routing only
        medium_severity_rule = aws_events.Rule(
            self,
            "MediumSeverityRule",
            rule_name=f"investigation-medium-severity-{environment_name}",
            description="Routes MEDIUM investigations to simple routing",
            event_bus=event_bus,
            event_pattern=aws_events.EventPattern(
                source=["devops.investigation"],
                detail_type=["InvestigationCompleted"],
                detail={
                    "severity": ["MEDIUM"]
                }
            )
        )
        medium_severity_rule.add_target(aws_events_targets.LambdaFunction(simple_routing_lambda))

        # Rule 3: All events → Pattern detection (for analysis)
        pattern_detection_rule = aws_events.Rule(
            self,
            "PatternDetectionRule",
            rule_name=f"investigation-pattern-detection-{environment_name}",
            description="Sends all investigations to pattern detection for correlation",
            event_bus=event_bus,
            event_pattern=aws_events.EventPattern(
                source=["devops.investigation"],
                detail_type=["InvestigationCompleted"]
            )
        )
        pattern_detection_rule.add_target(aws_events_targets.LambdaFunction(pattern_detection_lambda))

        # SQS DLQ for failed EventBridge invocations
        dlq = aws_sqs.Queue(
            self,
            "EventBridgeDLQ",
            queue_name=f"investigation-events-dlq-{environment_name}",
            retention_period=Duration.days(14),
            visibility_timeout=Duration.minutes(5)
        )
        
        # Add DLQ to high severity rule
        high_severity_rule.add_target(
            aws_events_targets.LambdaFunction(simple_routing_lambda, dead_letter_queue=aws_events.DeadLetterQueue(queue=dlq))
        )

        # CloudWatch dashboard
        dashboard = aws_cloudwatch.Dashboard(
            self,
            "InvestigationDashboard",
            dashboard_name=f"InvestigationOrchestrator-{environment_name}"
        )

        # EventBridge metrics
        dashboard.add_widgets(
            aws_cloudwatch.GraphWidget(
                title="EventBridge Events",
                left=[
                    event_bus.metric_all_event_count(),
                    aws_cloudwatch.Metric(
                        namespace="AWS/Events",
                        metric_name="FailedInvocations",
                        dimensions_map={"EventBusName": event_bus.event_bus_name}
                    )
                ],
                width=12
            )
        )

        # Lambda metrics
        dashboard.add_widgets(
            aws_cloudwatch.GraphWidget(
                title="Lambda Invocations",
                left=[
                    simple_routing_lambda.metric_invocations(),
                    pattern_detection_lambda.metric_invocations()
                ],
                width=12
            )
        )

        # DynamoDB metrics
        dashboard.add_widgets(
            aws_cloudwatch.GraphWidget(
                title="DynamoDB Operations",
                left=[
                    investigations_table.metric_consumed_write_capacity_units(),
                    investigations_table.metric_consumed_read_capacity_units()
                ],
                width=12
            )
        )

        # CloudWatch alarms
        
        # Alarm: High Lambda error rate
        simple_routing_error_alarm = aws_cloudwatch.Alarm(
            self,
            "SimpleRoutingErrorAlarm",
            alarm_name=f"investigation-simple-routing-errors-{environment_name}",
            metric=simple_routing_lambda.metric_errors(),
            threshold=5,
            evaluation_periods=1,
            comparison_operator=aws_cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            treat_missing_data=aws_cloudwatch.TreatMissingData.NOT_BREACHING
        )
        simple_routing_error_alarm.add_alarm_action(aws_cloudwatch_actions.SnsAction(alert_topic))

        # Alarm: EventBridge failed invocations
        eventbridge_failure_alarm = aws_cloudwatch.Alarm(
            self,
            "EventBridgeFailureAlarm",
            alarm_name=f"investigation-eventbridge-failures-{environment_name}",
            metric=aws_cloudwatch.Metric(
                namespace="AWS/Events",
                metric_name="FailedInvocations",
                dimensions_map={"EventBusName": event_bus.event_bus_name},
                statistic="Sum"
            ),
            threshold=3,
            evaluation_periods=1,
            comparison_operator=aws_cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD
        )
        eventbridge_failure_alarm.add_alarm_action(aws_cloudwatch_actions.SnsAction(alert_topic))

        # CloudFormation outputs
        CfnOutput(
            self,
            "EventBusArn",
            value=event_bus.event_bus_arn,
            description="EventBridge event bus ARN for client accounts",
            export_name=f"InvestigationEventBusArn-{environment_name}"
        )

        CfnOutput(
            self,
            "EventBusName",
            value=event_bus.event_bus_name,
            description="EventBridge event bus name"
        )

        CfnOutput(
            self,
            "InvestigationsTableName",
            value=investigations_table.table_name,
            description="DynamoDB table for investigation tracking"
        )

        CfnOutput(
            self,
            "AlertTopicArn",
            value=alert_topic.topic_arn,
            description="SNS topic for critical alerts"
        )


# CDK App
app = App()

if not central_account_id:
    raise ValueError("centralAccountId must be provided via context or CENTRAL_ACCOUNT_ID env var")

if not organization_id:
    raise ValueError("organizationId must be provided via context or ORGANIZATION_ID env var")

# Deploy central monitoring stack
CentralMonitoringStack(
    app,
    f"InvestigationOrchestrator-Central-{environment_name}",
    stack_name=f"investigation-orchestrator-central-{environment_name}",
    description="Central monitoring infrastructure for multi-client AWS DevOps Agent investigations",
    central_account_id=central_account_id,
    organization_id=organization_id,
    environment_name=environment_name,
    env={
        "account": central_account_id,
        "region": "us-east-1"
    },
    tags={
        "Project": "InvestigationOrchestrator",
        "Component": "Central",
        "Environment": environment_name,
        "ManagedBy": "CDK"
    }
)

app.synth()
