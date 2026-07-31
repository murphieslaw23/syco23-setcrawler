from app.schemas.import_job import ImportJob, JobType
from app.workers.celery_app import celery_app
from app.workers.provider_routing import dispatch_job


class JobDispatcher:
    def dispatch_url(self, job: ImportJob) -> None:
        dispatch_job(job)

    def dispatch_profile(self, job: ImportJob) -> None:
        dispatch_job(job)

    def retry(self, job: ImportJob) -> None:
        if job.job_type is JobType.search_profile:
            self.dispatch_profile(job)
        else:
            self.dispatch_url(job)
