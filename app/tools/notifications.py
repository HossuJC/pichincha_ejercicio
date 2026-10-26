import json
import logging
import threading
import uuid
from typing import Protocol

log = logging.getLogger(__name__)


class Notifier(Protocol):
    def notify_advisor(self, case: dict) -> str: ...


class OutboxNotifier:
    """Local notifier: keeps cases in memory and logs them. Idempotent per transaction."""

    def __init__(self):
        self._lock = threading.Lock()
        self.cases: dict[str, dict] = {}

    def notify_advisor(self, case: dict) -> str:
        key = f"{case['customer_id']}:{case.get('transaction_id') or case.get('reason')}"
        with self._lock:
            if key in self.cases:
                return self.cases[key]["case_id"]
            case_id = f"CASE-{uuid.uuid4().hex[:8].upper()}"
            self.cases[key] = {**case, "case_id": case_id}
        log.info("advisor notified: %s", json.dumps(self.cases[key], ensure_ascii=False))
        return case_id


class SNSNotifier:
    """AWS adapter: publishes the case to an SNS topic the advisors' queue subscribes to."""

    def __init__(self, topic_arn: str):
        import boto3  # only needed in AWS

        self._sns = boto3.client("sns")
        self._topic_arn = topic_arn

    def notify_advisor(self, case: dict) -> str:
        case_id = f"CASE-{uuid.uuid4().hex[:8].upper()}"
        self._sns.publish(
            TopicArn=self._topic_arn,
            Message=json.dumps({**case, "case_id": case_id}, ensure_ascii=False),
            MessageDeduplicationId=f"{case['customer_id']}:{case.get('transaction_id')}",
            MessageGroupId=case["customer_id"],
        )
        return case_id
