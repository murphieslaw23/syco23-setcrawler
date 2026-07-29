from app.schemas.import_job import ImportJob, JobType
from app.schemas.set import SetSource
from app.workers.celery_app import celery_app


_URL_TASKS = {
    SetSource.youtube: "app.workers.youtube_poller.import_url",
    SetSource.soundcloud: "app.workers.soundcloud_importer.import_url",
    SetSource.freeteknomusic: "app.workers.ftm_scraper.import_url",
}
_PROFILE_TASK = "app.workers.youtube_poller.poll_profile"


class JobDispatcher:
    def dispatch_url(self, job: ImportJob) -> None:
        celery_app.signature(_URL_TASKS[job.source]).delay(str(job.id))

    def dispatch_profile(self, job: ImportJob) -> None:
        celery_app.signature(_PROFILE_TASK).delay(str(job.id))

    def retry(self, job: ImportJob) -> None:
        if job.job_type is JobType.search_profile:
            self.dispatch_profile(job)
        else:
            self.dispatch_url(job)
