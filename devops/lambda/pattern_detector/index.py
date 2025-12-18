"""
Pattern Detection Lambda

Analyzes investigation patterns using Amazon Bedrock (10% of events).

This Lambda:
1. Queries recent investigations from DynamoDB
2. Detects patterns (multiple clients, same root cause)
3. Uses Bedrock Claude to analyze complex scenarios
4. Generates recommendations
5. Alerts senior engineers for correlated issues
"""

import json
import os
import boto3
from datetime import datetime, timedelta
from typing import List, Dict, Any

# AWS clients
dynamodb = boto3.resource('dynamodb')
bedrock = boto3.client('bedrock-runtime')
sns_client = boto3.client('sns')

# Environment variables
INVESTIGATIONS_TABLE_NAME = os.environ['INVESTIGATIONS_TABLE']
ALERT_TOPIC_ARN = os.environ['ALERT_TOPIC_ARN']
BEDROCK_MODEL_ID = os.environ['BEDROCK_MODEL_ID']
ENVIRONMENT = os.environ.get('ENVIRONMENT', 'dev')

# DynamoDB table
investigations_table = dynamodb.Table(INVESTIGATIONS_TABLE_NAME)


def handler(event, context):
    """
    Main handler for pattern detection
    
    Args:
        event: EventBridge event with investigation details
        context: Lambda context
    
    Returns:
        dict: Response with pattern analysis
    """
    print(f"Pattern detection triggered")
    
    try:
        # Store current investigation
        detail = event.get('detail', {})
        investigation_id = detail.get('investigation_id')
        
        if not investigation_id:
            raise ValueError("No investigation_id in event")
        
        # Query recent investigations (last 24 hours)
        recent_investigations = query_recent_investigations(hours=24)
        
        print(f"Found {len(recent_investigations)} recent investigations")
        
        # Check if pattern detection is needed
        if len(recent_investigations) < 3:
            print("Not enough investigations for pattern detection")
            return {
                'statusCode': 200,
                'body': json.dumps({'message': 'Insufficient data for pattern detection'})
            }
        
        # Analyze patterns with Bedrock
        analysis = analyze_patterns_with_bedrock(recent_investigations)
        
        # If patterns detected, alert senior engineer
        if analysis.get('patterns_detected'):
            alert_senior_engineer(analysis)
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Pattern analysis complete',
                'patterns_detected': analysis.get('patterns_detected', False),
                'investigation_id': investigation_id
            })
        }
        
    except Exception as e:
        print(f"Error in pattern detection: {str(e)}")
        raise


def query_recent_investigations(hours: int = 24) -> List[Dict[str, Any]]:
    """Query recent investigations from DynamoDB"""
    cutoff_time = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    
    # Query by timestamp (requires GSI or scan)
    response = investigations_table.scan(
        FilterExpression='#ts >= :cutoff',
        ExpressionAttributeNames={'#ts': 'timestamp'},
        ExpressionAttributeValues={':cutoff': cutoff_time}
    )
    
    return response.get('Items', [])


def analyze_patterns_with_bedrock(investigations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Analyze investigation patterns using Bedrock Claude
    
    Args:
        investigations: List of recent investigations
    
    Returns:
        dict: Analysis results with patterns and recommendations
    """
    # Prepare prompt for Bedrock
    prompt = build_analysis_prompt(investigations)
    
    # Call Bedrock
    try:
        response = bedrock.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            contentType='application/json',
            accept='application/json',
            body=json.dumps({
                'anthropic_version': 'bedrock-2023-05-31',
                'max_tokens': 2000,
                'messages': [
                    {
                        'role': 'user',
                        'content': prompt
                    }
                ]
            })
        )
        
        # Parse response
        response_body = json.loads(response['body'].read())
        analysis_text = response_body['content'][0]['text']
        
        # Parse structured response
        analysis = parse_bedrock_response(analysis_text)
        
        return analysis
        
    except Exception as e:
        print(f"Error calling Bedrock: {str(e)}")
        return {'patterns_detected': False, 'error': str(e)}


def build_analysis_prompt(investigations: List[Dict[str, Any]]) -> str:
    """Build prompt for Bedrock analysis"""
    investigations_summary = []
    
    for inv in investigations:
        investigations_summary.append({
            'client': inv['client_name'],
            'severity': inv['severity'],
            'root_cause_category': inv['summary']['root_cause_category'],
            'resource_types': inv['summary']['resource_types'],
            'timestamp': inv['timestamp']
        })
    
    prompt = f"""You are a senior cloud engineer analyzing incidents across multiple clients.

Recent investigations (last 24 hours):
{json.dumps(investigations_summary, indent=2)}

Tasks:
1. Identify if multiple clients are affected by the same underlying issue
2. Detect common patterns in root causes
3. Assess if this indicates a broader AWS service issue or regional problem
4. Provide actionable recommendations

Respond in JSON format:
{{
    "patterns_detected": boolean,
    "pattern_description": "string",
    "affected_clients": ["list of client names"],
    "recommended_actions": ["list of actions"],
    "escalation_needed": boolean,
    "confidence": "HIGH|MEDIUM|LOW"
}}"""
    
    return prompt


def parse_bedrock_response(response_text: str) -> Dict[str, Any]:
    """Parse Bedrock response into structured format"""
    try:
        # Remove markdown code blocks if present
        cleaned = response_text.strip()
        if cleaned.startswith('```json'):
            cleaned = cleaned[7:]
        if cleaned.endswith('```'):
            cleaned = cleaned[:-3]
        
        analysis = json.loads(cleaned.strip())
        return analysis
    except json.JSONDecodeError as e:
        print(f"Error parsing Bedrock response: {str(e)}")
        print(f"Response text: {response_text}")
        return {
            'patterns_detected': False,
            'error': 'Failed to parse Bedrock response'
        }


def alert_senior_engineer(analysis: Dict[str, Any]):
    """Alert senior engineer about detected patterns"""
    subject = f"[PATTERN DETECTED] Multiple Client Incident Correlation"
    
    message = f"""
Pattern Detection Alert
=======================

Pattern: {analysis.get('pattern_description', 'Unknown')}

Affected Clients: {', '.join(analysis.get('affected_clients', []))}

Recommended Actions:
{chr(10).join(f"- {action}" for action in analysis.get('recommended_actions', []))}

Escalation Needed: {analysis.get('escalation_needed', False)}
Confidence: {analysis.get('confidence', 'UNKNOWN')}

This indicates a potential broader issue affecting multiple clients.
Please review the investigations and coordinate response.
"""
    
    sns_client.publish(
        TopicArn=ALERT_TOPIC_ARN,
        Subject=subject,
        Message=message
    )
    
    print(f"Sent pattern detection alert to SNS")
