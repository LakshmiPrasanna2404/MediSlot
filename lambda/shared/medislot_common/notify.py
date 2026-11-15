"""Notifications through Amazon SNS, sent exactly once (architecture box 8).

One SNS topic, medislot-<env>-notifications. Every account subscribes its email
to it when it registers, with a filter policy on its own userId, so AWS sends a
confirmation email first (the "verified via SNS" step in the diagrams) and from
then on the account receives only messages published for it:

    publish  MessageAttributes {"userId": "<recipient>"}
    filter   {"userId": ["<recipient>"]}

Exactly once: before publishing, a conditional put of the dispatch key into the
dispatch-log table (attribute_not_exists). A second delivery of the same event,
or a retried reminder, finds the key and sends nothing. If SNS fails, the key is
deleted again so the retry can send.
"""

import json
import os
import time

from botocore.exceptions import ClientError

from . import config, logs

DISPATCH_TABLE = "DISPATCH_LOG_TABLE"


def topic_arn():
    return os.environ.get("NOTIFICATION_TOPIC_ARN", "")


def subscribe(email, user_id):
    """Subscribe an account's email to its own notifications. AWS emails a confirmation link first."""
    if not topic_arn():
        return None
    result = config.client("sns").subscribe(
        TopicArn=topic_arn(),
        Protocol="email",
        Endpoint=email,
        Attributes={"FilterPolicy": json.dumps({"userId": [user_id]})},
        ReturnSubscriptionArn=True,
    )
    return result.get("SubscriptionArn")


def send(dispatch_key, recipient_user_id, subject, message, event_type, entity_id):
    """Send once per dispatch key. Returns True if sent now, False if it had been sent before."""
    table = config.table(DISPATCH_TABLE)
    try:
        table.put_item(
            Item={
                "dispatchKey": dispatch_key,
                "entityId": entity_id,
                "eventType": event_type,
                "recipient": recipient_user_id,
                "createdAt": int(time.time()),
            },
            ConditionExpression="attribute_not_exists(dispatchKey)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logs.info("notification already sent", dispatchKey=dispatch_key, eventType=event_type)
            return False
        raise
    try:
        config.client("sns").publish(
            TopicArn=topic_arn(),
            Subject=subject[:100],
            Message=message,
            MessageAttributes={
                "userId": {"DataType": "String", "StringValue": recipient_user_id},
                "eventType": {"DataType": "String", "StringValue": event_type},
            },
        )
    except Exception:
        table.delete_item(Key={"dispatchKey": dispatch_key})  # let the retry send it
        raise
    logs.metric("NotificationsSent", EventType=event_type)
    logs.info("notification sent", dispatchKey=dispatch_key, eventType=event_type, recipient=recipient_user_id)
    return True
