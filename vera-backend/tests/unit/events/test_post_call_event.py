from uuid import uuid4

from vera_core.events.post_call import PostCallJob, parse_post_call_job


def test_post_call_job_roundtrips_json() -> None:
    job = PostCallJob(tenant_id=uuid4(), form_id=uuid4(), call_id=uuid4())
    restored = parse_post_call_job(job.model_dump_json())
    assert restored == job
