"""
Investigation Monitor Lambda

Single Lambda that handles the complete flow:
1. Polls AWS DevOps Agent for completed investigations (every 5 min)
2. Extracts summary information (no raw logs)
3. Redacts sensitive data (IPs, emails, secrets)
4. Generates signed URLs to DevOps Agent web app
5. Sends formatted event to central account EventBridge
6. Updates state in SSM Parameter Store

This consolidates what was previously two separate Lambdas for simplicity.
"""

import json
import os
import re
import boto3
from datetime import datetime, timedelta
from typing import List, Dict, Any

# AWS clients
events_client = boto3.client('events')
ssm_client = boto3.client('ssm')
logs_client = boto3.client('logs')

# Environment variables
CLIENT_NAME = os.environ['CLIENT_NAME']
CLIENT_ACCOUNT_ID = os.environ['CLIENT_ACCOUNT_ID']
CENTRAL_EVENT_BUS_ARN = os.environ['CENTRAL_EVENT_BUS_ARN']
DEVOPS_AGENT_SPACE_ID = os.environ.get('DEVOPS_AGENT_SPACE_ID', '')
DEVOPS_AGENT_REGION = os.environ.get('DEVOPS_AGENT_REGION', 'us-east-1')
STATE_PARAMETER_NAME = os.environ['STATE_PARAMETER_NAME']
ENVIRONMENT = os.environ.get('ENVIRONMENT', 'dev')
TAGS = eval(os.environ.get('TAGS', '{}'))

# Regex patterns for redaction
IP_PATTERN = re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b')
EMAIL_PATTERN = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
SECRET_PATTERN = re.compile(r'(password|secret|key|token)[\s:=]+[^\s]+', re.IGNORECASE)


def handler(event, context):
    """
    Main handler for investigation monitor
    
    Args:
        event: EventBridge scheduled event
        context: Lambda context
    
    Returns:
        dict: Response with processed investigations count
    """
    print(f"Investigation monitor triggered for {CLIENT_NAME}")
    
    try:
        # Step 1: Get last processed timestamp from state
        last_processed_time = get_last_processed_time()
        print(f"Last processed time: {last_processed_time}")
        
        # Step 2: Query DevOps Agent for completed investigations
        completed_investigations = get_completed_investigations(last_processed_time)
        
        if not completed_investigations:
            print("No new completed investigations found")
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': 'No new investigations',
                    'client': CLIENT_NAME
                })
            }
        
        print(f"Found {len(completed_investigations)} completed investigations")
        
        # Step 3: Process each completed investigation
        processed_count = 0
        for investigation in completed_investigations:
            try:
                # Format the investigation event
                formatted_event = format_investigation_event(investigation)
                
                # Send to central EventBridge
                send_to_central_eventbridge(formatted_event)
                
                processed_count += 1
            except Exception as e:
                print(f"Error processing investigation {investigation['investigation_id']}: {str(e)}")
                # Continue processing other investigations
        
        # Step 4: Update state with latest processed time
        if completed_investigations:
            latest_time = max(inv['completed_at'] for inv in completed_investigations)
            update_last_processed_time(latest_time)
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': f'Processed {processed_count} investigations',
                'client': CLIENT_NAME,
                'processed_count': processed_count
            })
        }
        
    except Exception as e:
        print(f"Error in investigation monitor: {str(e)}")
        raise


# ===== STATE MANAGEMENT =====

def get_last_processed_time() -> str:
    """Get last processed timestamp from SSM Parameter Store"""
    try:
        response = ssm_client.get_parameter(Name=STATE_PARAMETER_NAME)
        return response['Parameter']['Value']
    except ssm_client.exceptions.ParameterNotFound:
        # First run, use 1 hour ago as default
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)
        return one_hour_ago.isoformat()


def update_last_processed_time(timestamp: str):
    """Update last processed timestamp in SSM Parameter Store"""
    ssm_client.put_parameter(
        Name=STATE_PARAMETER_NAME,
        Value=timestamp,
        Type='String',
        Overwrite=True,
        Description=f'Last processed investigation timestamp for {CLIENT_NAME}'
    )


# ===== DEVOPS AGENT POLLING =====

def get_completed_investigations(since_time: str) -> List[Dict[str, Any]]:
    """
    Query DevOps Agent for completed investigations
    
    NOTE: During AWS DevOps Agent preview, the API may not be fully available.
    This implementation uses CloudWatch Logs as a fallback.
    
    Args:
        since_time: ISO timestamp of last processed investigation
    
    Returns:
        List of completed investigations
    """
    investigations = []
    
    try:
        # Parse CloudWatch Logs for DevOps Agent activity
        log_group_name = '/aws/devops-agent/investigations'
        
        # Convert since_time to timestamp
        since_timestamp = int(datetime.fromisoformat(since_time.replace('Z', '')).timestamp() * 1000)
        
        response = logs_client.filter_log_events(
            logGroupName=log_group_name,
            startTime=since_timestamp,
            filterPattern='investigation_completed'
        )
        
        for event in response.get('events', []):
            message = event.get('message', '')
            
            # Parse investigation completion from log message
            try:
                investigation_data = json.loads(message)
                if investigation_data.get('status') == 'COMPLETED':
                    investigations.append({
                        'investigation_id': investigation_data.get('investigation_id'),
                        'severity': investigation_data.get('severity', 'MEDIUM'),
                        'root_cause': investigation_data.get('root_cause', ''),
                        'affected_resources': investigation_data.get('affected_resources', []),
                        'completed_at': datetime.utcfromtimestamp(event['timestamp'] / 1000).isoformat(),
                        'duration_minutes': investigation_data.get('duration_minutes', 0)
                    })
            except json.JSONDecodeError:
                print(f"Could not parse log message: {message}")
                continue
        
    except logs_client.exceptions.ResourceNotFoundException:
        print(f"Log group not found: {log_group_name}")
        print("This is expected if DevOps Agent hasn't created logs yet")
    except Exception as e:
        print(f"Error querying CloudWatch Logs: {str(e)}")
    
    return investigations


# ===== EVENT FORMATTING =====

def format_investigation_event(investigation: Dict[str, Any]) -> Dict[str, Any]:
    """
    Format investigation into event schema for central account
    
    Args:
        investigation: Raw investigation data
    
    Returns:
        Formatted event dictionary
    """
    investigation_id = investigation.get('investigation_id', 'unknown')
    
    # Extract summary information only
    summary = {
        'affected_resources': investigation.get('affected_resources', [])[:5],  # Limit to 5
        'resource_types': extract_resource_types(investigation.get('affected_resources', [])),
        'duration_minutes': investigation.get('duration_minutes', 0),
        'root_cause_category': categorize_root_cause(investigation.get('root_cause', '')),
        'root_cause_brief': redact_sensitive_data(
            investigation.get('root_cause', '')[:200]  # Truncate to 200 chars
        ),
        'mitigation_status': 'plan_generated'
    }
    
    # Generate links (no actual data)
    links = {
        'devops_agent_investigation': generate_devops_agent_link(investigation_id),
        'cloudwatch_logs': generate_cloudwatch_logs_link(),
        'affected_application': TAGS.get('application_url', '')
    }
    
    # Build event
    event = {
        'event_type': 'investigation_completed',
        'investigation_id': investigation_id,
        'client_account_id': CLIENT_ACCOUNT_ID,
        'client_name': CLIENT_NAME,
        'timestamp': investigation.get('completed_at', datetime.utcnow().isoformat()),
        'severity': investigation.get('severity', 'MEDIUM'),
        'status': 'ROOT_CAUSE_FOUND',
        'summary': summary,
        'links': links,
        'tags': TAGS
    }
    
    # Validate event size
    event_size = len(json.dumps(event).encode('utf-8'))
    if event_size > 200_000:  # 200 KB warning
        print(f"WARNING: Event size {event_size} bytes is large")
    
    if event_size > 256_000:  # 256 KB hard limit
        raise ValueError(f"Event size {event_size} bytes exceeds EventBridge limit")
    
    return event


def extract_resource_types(resources: list) -> list:
    """Extract unique resource types from ARNs"""
    types = set()
    for resource in resources:
        if isinstance(resource, str) and resource.startswith('arn:'):
            # ARN format: arn:partition:service:region:account:resource
            parts = resource.split(':')
            if len(parts) >= 6:
                service = parts[2]
                types.add(service.upper())
    return list(types)


def categorize_root_cause(root_cause: str) -> str:
    """Categorize root cause into high-level categories"""
    root_cause_lower = root_cause.lower()
    
    if any(keyword in root_cause_lower for keyword in ['connection', 'timeout', 'network']):
        return 'network_connectivity'
    elif any(keyword in root_cause_lower for keyword in ['memory', 'cpu', 'disk', 'capacity']):
        return 'resource_exhaustion'
    elif any(keyword in root_cause_lower for keyword in ['permission', 'access', 'denied', 'unauthorized']):
        return 'permissions_issue'
    elif any(keyword in root_cause_lower for keyword in ['deployment', 'version', 'rollout']):
        return 'deployment_issue'
    elif any(keyword in root_cause_lower for keyword in ['dependency', 'service', 'downstream']):
        return 'dependency_failure'
    else:
        return 'unknown'


def redact_sensitive_data(text: str) -> str:
    """Redact sensitive data from text"""
    # Redact IP addresses
    text = IP_PATTERN.sub('REDACTED_IP', text)
    
    # Redact email addresses
    text = EMAIL_PATTERN.sub('REDACTED_EMAIL', text)
    
    # Redact secrets/passwords
    text = SECRET_PATTERN.sub(r'\1: REDACTED', text)
    
    return text


def generate_devops_agent_link(investigation_id: str) -> str:
    """Generate link to DevOps Agent investigation"""
    region = os.environ.get('AWS_REGION', 'us-east-1')
    return f"https://devops-agent.console.aws.amazon.com/spaces/{DEVOPS_AGENT_SPACE_ID}/investigations/{investigation_id}?region={region}"


def generate_cloudwatch_logs_link() -> str:
    """Generate link to CloudWatch Logs"""
    region = os.environ.get('AWS_REGION', 'us-east-1')
    log_group = '/aws/devops-agent/investigations'
    return f"https://console.aws.amazon.com/cloudwatch/home?region={region}#logsV2:log-groups/log-group/{log_group.replace('/', '$252F')}"


# ===== SEND TO CENTRAL =====

def send_to_central_eventbridge(event: Dict[str, Any]):
    """
    Send formatted event to central account EventBridge
    
    Args:
        event: Formatted investigation event
    """
    response = events_client.put_events(
        Entries=[{
            'Source': 'devops.investigation',
            'DetailType': 'InvestigationCompleted',
            'Detail': json.dumps(event),
            'EventBusName': CENTRAL_EVENT_BUS_ARN
        }]
    )
    
    # Check for failures
    if response['FailedEntryCount'] > 0:
        failed_entries = [e for e in response['Entries'] if 'ErrorCode' in e]
        raise Exception(f"Failed to send events: {failed_entries}")
    
    print(f"Successfully sent event to central EventBridge: {event['investigation_id']}")
    print(f"Event size: {len(json.dumps(event))} bytes")
