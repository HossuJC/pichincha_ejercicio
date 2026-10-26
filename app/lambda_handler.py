"""AWS Lambda entry point behind API Gateway.

In Lambda, background threads are frozen between invocations, so a legacy call that
exceeds the timeout must be completed by a worker (SQS -> Lambda) rather than the
in-process thread used locally; see infra/template.yaml.
"""

from mangum import Mangum

from app.main import create_app

handler = Mangum(create_app(), lifespan="off")
