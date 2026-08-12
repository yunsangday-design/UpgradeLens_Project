"""Celery 4.x task definitions — uses APIs that break in Celery 5.x."""
from celery import Celery
from celery.task import periodic_task
from celery.utils.log import get_task_logger
from datetime import timedelta

# Celery 4.x app configuration style
app = Celery("worker")
app.config_from_object({
    "CELERY_BROKER_URL": "redis://localhost:6379/0",
    "CELERY_RESULT_BACKEND": "redis://localhost:6379/1",
    "CELERY_TASK_SERIALIZER": "json",
    "CELERY_ACCEPT_CONTENT": ["json"],
    "CELERY_TIMEZONE": "UTC",
    "CELERY_ENABLE_UTC": True,
    # Celery 4.x uppercase settings
    "CELERY_TASK_ALWAYS_EAGER": False,
    "CELERY_TASK_EAGER_PROPAGATES": True,
    "CELERYD_PREFETCH_MULTIPLIER": 4,
    "CELERY_TASK_RESULT_EXPIRES": 3600,
})

logger = get_task_logger(__name__)


@app.task(bind=True, max_retries=3)
def send_email(self, to_addr, subject, body):
    """Send an email, retry on failure."""
    try:
        logger.info(f"Sending email to {to_addr}")
        # simulate sending
        return {"status": "sent", "to": to_addr}
    except Exception as exc:
        raise self.retry(exc=exc, countdown=60)


@periodic_task(run_every=timedelta(minutes=30))
def cleanup_expired_sessions():
    """Periodic task using deprecated @periodic_task decorator."""
    logger.info("Cleaning up expired sessions")
    return {"cleaned": 42}


@app.task(name="worker.process_payment")
def process_payment(order_id, amount):
    """Process a payment."""
    logger.info(f"Processing payment for order {order_id}: ${amount}")
    return {"order_id": order_id, "status": "processed"}


# Celery 4.x: using app.send_task with old-style routing
def dispatch_task(task_name, args, queue="default"):
    """Dispatch a task to a specific queue."""
    return app.send_task(
        task_name,
        args=args,
        queue=queue,
        routing_key=f"{queue}.priority",
    )


# Celery 4.x chord/group usage
from celery import chord, group

def run_batch_pipeline(items):
    """Run a batch of tasks followed by a callback."""
    header = group(process_payment.s(item["id"], item["amount"]) for item in items)
    callback = send_email.s("admin@example.com", "Batch done", "All payments processed")
    result = chord(header)(callback)
    return result


# Celery 4.x: kombu-level settings (break in 5.x)
app.conf.update(
    BROKER_TRANSPORT_OPTIONS={
        "visibility_timeout": 3600,
        "fanout_prefix": True,
        "fanout_patterns": True,
    }
)
