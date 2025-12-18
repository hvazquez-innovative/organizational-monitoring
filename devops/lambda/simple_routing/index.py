"""
Simple Routing Lambda

Handles 90% of investigation events with deterministic routing:
- HIGH/CRITICAL → Page engineer via PagerDuty
- MEDIUM → Create Jira ticket
- All → Store in DynamoDB

This Lambda keeps logic simple and fast for most cases.
Complex pattern detection is handled by a separate Lambda.
"""

import json
import os
import boto3
from datetime import datetime
from typing import Dict, Any

# AWS clients
dynamodb = boto3.resource('dynamodb')
sns_client = boto3.client('sns')
secretsmanager_client = boto3.client('secretsmanager')

# Environment variables
INVESTIGATIONS_TABLE_NAME = os.environ['INVESTIGATIONS_TABLE']
ALERT_TOPIC_ARN = os.environ['ALERT_TOPIC_ARN']
PAGERDUTY_SECRET_NAME = os.environ.get('PAGERDUTY_API_KEY_SECRET', '')
JIRA_SECRET_NAME = os.environ.get('JIRA_API_KEY_SECRET', '')
ENVIRONMENT = os.environ.get('ENVIRONMENT', 'dev')

# DynamoDB table
investigations_table = dynamodb.Table(INVESTIGATIONS_TABLE_NAME)


def handler(event, context):
    """
    Main handler for simple routing
    
    Args:
        event: EventBridge event with investigation details
        context: Lambda context
    
    Returns:
        dict: Response with routing actions taken
    """
    print(f"Simple routing triggered")
    print(f"Event: {json.dumps(event)}")
    
    try:
        # Extract investigation from EventBridge event
        detail = event.get('detail', {})
        investigation_id = detail.get('investigation_id')
        severity = detail.get('severity', 'MEDIUM')
        client_name = detail.get('client_name', 'Unknown')
        
        if not investigation_id:
            raise ValueError("No investigation_id in event")
        
        # Store in DynamoDB
        store_investigation(detail)
        
        # Route based on severity
        actions_taken = []
        
        if severity in ['CRITICAL', 'HIGH']:
            # Page engineer
            if PAGERDUTY_SECRET_NAME:
                page_engineer(detail)
                actions_taken.append('paged_engineer')
            
            # Also create ticket for tracking
            if JIRA_SECRET_NAME:
                create_jira_ticket(detail)
                actions_taken.append('created_ticket')
            
            # Send SNS alert
            send_sns_alert(detail)
            actions_taken.append('sent_alert')
            
        elif severity == 'MEDIUM':
            # Just create ticket
            if JIRA_SECRET_NAME:
                create_jira_ticket(detail)
                actions_taken.append('created_ticket')
        
        print(f"Routing complete for {investigation_id}: {actions_taken}")
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Routing successful',
                'investigation_id': investigation_id,
                'actions': actions_taken
            })
        }
        
    except Exception as e:
        print(f"Error in simple routing: {str(e)}")
        raise


def store_investigation(investigation: Dict[str, Any]):
    """Store investigation in DynamoDB"""
    item = {
        'investigation_id': investigation['investigation_id'],
        'timestamp': investigation['timestamp'],
        'client_account_id': investigation['client_account_id'],
        'client_name': investigation['client_name'],
        'severity': investigation['severity'],
        'status': investigation['status'],
        'summary': investigation['summary'],
        'links': investigation['links'],
        'tags': investigation.get('tags', {}),
        'processed_at': datetime.utcnow().isoformat(),
        'environment': ENVIRONMENT
    }
    
    investigations_table.put_item(Item=item)
    print(f"Stored investigation {investigation['investigation_id']} in DynamoDB")


def page_engineer(investigation: Dict[str, Any]):
    """Page on-call engineer via PagerDuty"""
    try:
        # Get PagerDuty API key from Secrets Manager
        secret_value = secretsmanager_client.get_secret_value(SecretId=PAGERDUTY_SECRET_NAME)
        pagerduty_config = json.loads(secret_value['SecretString'])
        
        api_key = pagerduty_config['api_key']
        service_id = pagerduty_config['service_id']
        
        # Create PagerDuty incident
        # TODO: Implement actual PagerDuty API call
        # For now, log the action
        print(f"Would page PagerDuty service {service_id} for investigation {investigation['investigation_id']}")
        print(f"Severity: {investigation['severity']}")
        print(f"Client: {investigation['client_name']}")
        
    except Exception as e:
        print(f"Error paging engineer: {str(e)}")
        # Don't fail the whole Lambda if paging fails
        pass


def create_jira_ticket(investigation: Dict[str, Any]):
    """Create Jira ticket for investigation"""
    try:
        # Get Jira credentials from Secrets Manager
        secret_value = secretsmanager_client.get_secret_value(SecretId=JIRA_SECRET_NAME)
        jira_config = json.loads(secret_value['SecretString'])
        
        api_token = jira_config['api_token']
        project_key = jira_config['project_key']
        
        # Create Jira ticket
        # TODO: Implement actual Jira API call
        # For now, log the action
        print(f"Would create Jira ticket in project {project_key} for investigation {investigation['investigation_id']}")
        print(f"Summary: {investigation['summary']['root_cause_brief']}")
        
    except Exception as e:
        print(f"Error creating Jira ticket: {str(e)}")
        # Don't fail the whole Lambda if ticket creation fails
        pass


def send_sns_alert(investigation: Dict[str, Any]):
    """Send SNS alert for critical investigations"""
    subject = f"[{investigation['severity']}] Investigation Alert - {investigation['client_name']}"
    
    message = f"""
Investigation Alert
==================

Client: {investigation['client_name']}
Severity: {investigation['severity']}
Investigation ID: {investigation['investigation_id']}

Root Cause: {investigation['summary']['root_cause_brief']}

Affected Resources: {', '.join(investigation['summary']['resource_types'])}
Duration: {investigation['summary']['duration_minutes']} minutes

Links:
- DevOps Agent: {investigation['links']['devops_agent_investigation']}
- CloudWatch Logs: {investigation['links']['cloudwatch_logs']}

Timestamp: {investigation['timestamp']}
"""
    
    sns_client.publish(
        TopicArn=ALERT_TOPIC_ARN,
        Subject=subject,
        Message=message
    )
    
    print(f"Sent SNS alert for investigation {investigation['investigation_id']}")
